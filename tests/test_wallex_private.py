"""WallexBroker / WallexAccountReader: fake transports only (no network) and FAKE keys only.

Every Wallex response here is HAND-WRITTEN to the shapes the adapter assumes (listed in
wallexbot.exchange.wallex_private); no private response has ever been captured. These tests pin
the adapter's behaviour and its fail-closed edges, not Wallex's real API.
"""
from __future__ import annotations

import ast
import email.message
import http.client
import importlib.util
import inspect
import io
import json
import logging
import urllib.error
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from wallexbot.exchange import auth as A
from wallexbot.exchange import wallex_private as WP
from wallexbot.exchange.auth import (AuthError, UrlNotAllowed, assert_host_allowed,
                                     assert_live_armed, is_live_armed_token)
from wallexbot.exchange.base import (Balance, Broker, BrokerError, BrokerTimeout, DuplicateClientId,
                                     FeeRates, OrderRejected, OrderStatus, RateLimited)
from wallexbot.exchange.ratelimit import TokenBucket
from wallexbot.exchange.wallex_private import (AuthRejected, BrokerDataError, BrokerHTTPError,
                                               WallexAccountReader, WallexBroker, parse_balances,
                                               parse_fee_table, parse_order)
from wallexbot.exchange.wallex_public import WallexDataError
from wallexbot.oms import OMS
from wallexbot import redact as R
from wallexbot.pipeline import auto_approve, snapshot_account
from wallexbot.redact import Secret
from wallexbot.risk.engine import RiskEngine
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import CLIENT_ID_RE
from wallexbot.types import IntentState, OrderIntent, OrderType, Side

D = Decimal
S = IntentState
SYM = "BTCUSDT"
BASE = "https://api.wallex.ir"
# No real words inside the fake values: the scanner flags any 6-char run of them, and a run like
# "read_k" would also match the field name "read_key" in a (correctly masked) repr.
FAKE_READ_KEY = "FAKEtestRd7Qz9Wm2Xc5Vb8Nn3Lp"
FAKE_TRADE_KEY = "FAKEtestTr4Jd6Ys3Kr8Ue2Gt7Hq"
FAKE_SUB = "FAKEtestSb7f3Kq9Zx"
ARMED_ENV = {"WALLEX_ARM_LIVE": "YES_I_UNDERSTAND", "WALLEX_API_KEY_TRADE": FAKE_TRADE_KEY}
FOREIGN_CID = "d4a5ed1e-b1e6-4c13-8b9d-a21e2a6b8fd0"     # an id Wallex might give a manual order


def intent(sig="sig-1", qty="0.010000", price="60010.00", type=OrderType.LIMIT, side=Side.BUY,
           reduce_only=False):
    return OrderIntent(SYM, side, type, D(qty), None if price is None else D(price), sig,
                       reduce_only=reduce_only)


CID = intent().client_id
OTHER_CID = intent(sig="sig-2").client_id


def _load_scanner():
    """scan_text_for_secrets from tests/test_no_secrets_in_repo.py, imported by path."""
    spec = importlib.util.spec_from_file_location(
        "_wallexbot_secret_scanner", Path(__file__).with_name("test_no_secrets_in_repo.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.scan_text_for_secrets


scan_text_for_secrets = _load_scanner()


# ----------------------------------------------------------------------------- fakes and builders
@dataclass
class Call:
    method: str
    url: str
    headers: dict
    headers_repr: str
    body: bytes | None
    timeout: float


class Scripted:
    """A transport that answers with queued responses (or raises queued exceptions), in order."""

    def __init__(self, *responses):
        self.queue = deque(responses)
        self.calls: list[Call] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(Call(method, url, dict(headers), repr(headers), body, timeout))
        assert self.queue, f"unexpected request: {method} {url}"
        r = self.queue.popleft()
        if isinstance(r, BaseException):
            raise r
        return r


def envelope(result, *, success=True, message="The operation was successful"):
    return json.dumps({"result": result, "message": message, "success": success}).encode()


def ok(result, status=200, headers=None):
    return status, dict(headers or {"Content-Type": "application/json"}), envelope(result)


def fail(status, message, **errors):
    body = {"success": False, "message": message, "code": status, "result": errors}
    return status, {"Content-Type": "application/json"}, json.dumps(body).encode()


def raw(status, body=b"", headers=None):
    return status, dict(headers or {}), body


def w16(x) -> str:
    """Wallex-style fixed 16-decimal string."""
    return format(D(str(x)), ".16f")


def fill_json(price, qty, fee, asset, symbol=SYM):
    return {"price": w16(price), "quantity": w16(qty), "fee": w16(fee), "feeCoefficient": "0.0035",
            "feeAsset": asset, "timestamp": "2026-10-22T08:00:01Z", "symbol": symbol,
            "sum": w16(D(price) * D(qty)), "isBuyer": True}


def order_json(cid=CID, *, symbol=SYM, side="BUY", type_="LIMIT", qty="0.01", price="60010",
               status="NEW", fills=(), **extra):
    """Its stop price is under ``stopPrice`` (the RESPONSE spelling; the create request uses
    ``stop_Price``)."""
    fills = [fill_json(*f) if isinstance(f, tuple) else f for f in fills]
    executed = sum((D(f["quantity"]) for f in fills), D(0))
    executed_sum = sum((D(f["price"]) * D(f["quantity"]) for f in fills), D(0))
    o = {"symbol": symbol, "type": type_, "side": side, "clientOrderId": cid,
         "transactTime": 1760000000, "price": w16(price) if price is not None else "0",
         "origQty": w16(qty), "executedSum": w16(executed_sum), "executedQty": w16(executed),
         "executedPrice": w16(executed_sum / executed if executed else 0),
         "sum": w16(D(qty) * D(price or 0)), "executedPercent": int(executed / D(qty) * 100),
         "stopPrice": None, "stopPriceCondition": None,
         "status": status, "active": status in ("NEW", "PARTIALLY_FILLED"), "fills": fills}
    o.update(extra)
    return o


@pytest.fixture(autouse=True)
def isolated_registry():
    """Secrets created by a test (e.g. a malformed fake key) stay registered only for that test."""
    saved = set(R._SECRETS)
    R._reset_registry()
    yield
    R._reset_registry(saved)


@pytest.fixture
def armed():
    """The arming token: the object assert_live_armed() returns when every condition holds."""
    return assert_live_armed(dict(ARMED_ENV), True)


def broker(transport, armed=None, *, read=True, trade=True, **kw):
    kw.setdefault("sleep", lambda s: None)
    trade_key = (armed if armed is not None else Secret(FAKE_TRADE_KEY)) if trade else None
    return WallexBroker(Secret(FAKE_READ_KEY) if read else None, trade_key, transport=transport,
                        live_armed=armed, **kw)


def place(b, cid=CID, type=OrderType.LIMIT, price=D("60010.00"), qty=D("0.010000"), **kw):
    return b.submit_order(client_id=cid, symbol=SYM, side=Side.BUY, type=type, quantity=qty,
                          price=price, **kw)


# ----------------------------------------------------------------------------- request construction
def test_submit_limit_is_one_post_with_the_trade_key_and_string_values(armed):
    t = Scripted(ok(order_json(), status=201))
    b = broker(t, armed, sub_account=Secret(FAKE_SUB))
    order = place(b)
    assert len(t.calls) == 1
    c = t.calls[0]
    assert (c.method, c.url, c.timeout) == ("POST", f"{BASE}/v1/account/orders", 10.0)
    assert c.headers["X-API-Key"] == FAKE_TRADE_KEY and c.headers["sub-account-client-id"] == FAKE_SUB
    assert c.headers["Content-Type"] == "application/json"
    body = json.loads(c.body)
    assert body == {"symbol": SYM, "side": "BUY", "type": "LIMIT", "quantity": "0.01",
                    "price": "60010", "client_id": CID}
    assert all(type(v) is str for v in body.values())
    for key in (FAKE_TRADE_KEY, FAKE_READ_KEY):
        assert key not in c.url and key.encode() not in c.body and key not in c.headers_repr
    assert (order.client_id, order.status, order.filled_qty, order.quantity) == \
        (CID, OrderStatus.NEW, 0, D("0.01"))


def test_market_order_never_sends_a_price(armed):
    t = Scripted(ok(order_json(type_="MARKET", price=None), status=201))
    order = place(broker(t, armed), type=OrderType.MARKET, price=D("60010"))
    assert "price" not in json.loads(t.calls[0].body) and order.price is None


def test_stop_orders_are_refused_unless_enabled_then_use_the_stop_field(armed):
    t = Scripted()
    with pytest.raises(OrderRejected, match="stop orders are disabled"):
        place(broker(t, armed), type=OrderType.STOP_LIMIT, price=D("59000"), stop_price=D("59100"))
    assert t.calls == []
    t = Scripted(ok(order_json(type_="STOP_LIMIT", price="59000", stopPrice=w16("59100")), 201))
    order = place(broker(t, armed, allow_stop_orders=True), type=OrderType.STOP_LIMIT,
                  price=D("59000"), stop_price=D("59100"))
    body = json.loads(t.calls[0].body)
    assert WP.STOP_PRICE_FIELD == "stop_Price"              # documented request spelling
    assert WP.STOP_PRICE_RESPONSE_FIELD == "stopPrice"      # documented response spelling
    assert body["stop_Price"] == "59100" and body["type"] == "STOP_LIMIT" and body["price"] == "59000"
    assert "stopPrice" not in body
    assert order.stop_price == D("59100")


def test_response_stop_price_is_read_only_from_the_documented_stopPrice_field():
    stop = order_json(type_="STOP_MARKET", price=None, stopPrice=w16("59100"))
    assert parse_order(stop, now=0.0).stop_price == D("59100")
    request_spelling = order_json(type_="STOP_MARKET", price=None, stop_Price=w16("59100"))
    assert parse_order(request_spelling, now=0.0).stop_price is None
    for value in ("0", "-1", "x", 59100.5):                 # a stop order needs a positive stop
        with pytest.raises(BrokerDataError):
            parse_order(order_json(type_="STOP_LIMIT", price="59000", stopPrice=value), now=0.0)
    limit = order_json(stopPrice="0", stopPriceCondition="")    # ignored on a non-stop order
    assert parse_order(limit, now=0.0).stop_price is None


def test_non_stop_order_never_sends_a_stop_field(armed):
    t = Scripted(ok(order_json(), 201))
    place(broker(t, armed, allow_stop_orders=True), stop_price=D("55000"))
    assert "stop_Price" not in json.loads(t.calls[0].body)


@pytest.mark.parametrize("kwargs", [
    {"cid": "wb_NOT_HEX_" + "0" * 22}, {"cid": "../orders"}, {"qty": 0.01}, {"qty": D("0")},
    {"qty": D("-1")}, {"qty": D("NaN")}, {"price": None}, {"price": D("0")}, {"price": "60010"},
    {"qty": D("1E+50")},
])
def test_invalid_order_arguments_are_rejected_before_any_io(armed, kwargs):
    t = Scripted()
    with pytest.raises(OrderRejected):
        place(broker(t, armed), **kwargs)
    assert t.calls == []


def test_invalid_symbol_side_or_type_is_rejected_before_any_io(armed):
    t = Scripted()
    b = broker(t, armed)
    for kw in ({"symbol": "btc/usdt"}, {"side": "BUY"}, {"type": "LIMIT"}):
        args = dict(client_id=CID, symbol=SYM, side=Side.BUY, type=OrderType.LIMIT,
                    quantity=D("0.01"), price=D("60010"))
        args.update(kw)
        with pytest.raises(OrderRejected):
            b.submit_order(**args)
    assert t.calls == []


def test_get_order_is_a_get_with_the_read_key(armed):
    t = Scripted(ok(order_json()))
    broker(t, armed).get_order(CID)
    c = t.calls[0]
    assert (c.method, c.url, c.body) == ("GET", f"{BASE}/v1/account/orders/{CID}", None)
    assert c.headers["X-API-Key"] == FAKE_READ_KEY


def test_every_private_request_sends_the_documented_json_content_type(armed):
    """The docs ask for Content-Type: application/json on every request, bodiless ones too."""
    t = Scripted(ok(order_json(), 201), ok(order_json()), ok({"orders": []}),
                 ok({"balances": {}}), ok({SYM: {"makerFeeRate": "0.002", "takerFeeRate": "0.002"}}),
                 ok({}), ok(order_json(status="CANCELED")))
    b = broker(t, armed, sub_account=Secret(FAKE_SUB))
    place(b)
    b.get_order(CID)
    b.open_orders()
    b.balances()
    b.fee_rates()
    b.cancel_order(CID)
    assert [c.method for c in t.calls] == ["POST"] + ["GET"] * 4 + ["DELETE", "GET"]
    for c in t.calls:
        assert c.headers["Content-Type"] == "application/json"
        assert c.headers["Accept"] == "application/json"
        assert c.headers["sub-account-client-id"] == FAKE_SUB
        assert (c.body is None) is (c.method != "POST")


def test_reads_fall_back_to_the_trade_key_only_without_a_read_key(armed):
    t = Scripted(ok({"balances": {}}), ok({"balances": {}}))
    broker(t, armed, read=False).balances()
    broker(t, armed).balances()
    assert [c.headers["X-API-Key"] for c in t.calls] == [FAKE_TRADE_KEY, FAKE_READ_KEY]


def test_cancel_is_a_delete_with_the_trade_key_then_a_read_back(armed):
    t = Scripted(ok({}), ok(order_json(status="CANCELED")))
    order = broker(t, armed).cancel_order(CID)
    url = f"{BASE}/v1/account/orders/{CID}"
    assert [(c.method, c.url, c.body) for c in t.calls] == [("DELETE", url, None), ("GET", url, None)]
    assert [c.headers["X-API-Key"] for c in t.calls] == [FAKE_TRADE_KEY, FAKE_READ_KEY]
    assert order.status is OrderStatus.CANCELED


def test_refused_cancel_returns_a_final_or_unknown_order_but_raises_while_open(armed):
    done = order_json(status="FILLED", fills=[("60010", "0.01", "0.000035", "BTC")])
    assert broker(Scripted(fail(422, "Order is not open."), ok(done)), armed) \
        .cancel_order(CID).status is OrderStatus.FILLED
    gone = fail(404, "Order not found.")
    assert broker(Scripted(gone, gone), armed).cancel_order(CID) is None
    t = Scripted(raw(404, b"<html>404 Not Found</html>"), ok(order_json(status="NEW")))
    with pytest.raises(BrokerHTTPError, match="still open"):
        broker(t, armed).cancel_order(CID)      # e.g. a wrong cancel route must not look like success


def test_open_orders_pages_through_all_pages(armed):
    page1 = [order_json(intent(sig=f"s{i}").client_id) for i in range(WP.PER_PAGE)]
    page2 = [order_json(CID), order_json(OTHER_CID, symbol="ETHUSDT")]
    t = Scripted(ok({"orders": page1}), ok({"orders": page2}))
    orders = broker(t, armed).open_orders()
    assert len(orders) == WP.PER_PAGE + 2 and len(t.calls) == 2
    queries = [parse_qs(urlsplit(c.url).query) for c in t.calls]
    assert queries == [{"page": ["1"], "per_page": ["100"]}, {"page": ["2"], "per_page": ["100"]}]
    assert all(urlsplit(c.url).path == "/v1/account/openOrders" and c.method == "GET" for c in t.calls)
    t = Scripted(ok({"orders": page2}))
    only = broker(t, armed).open_orders(SYM)
    assert [o.client_id for o in only] == [CID]                       # filtered client-side too
    assert parse_qs(urlsplit(t.calls[0].url).query)["symbol"] == [SYM]


def test_open_orders_stops_when_paging_is_ignored_and_refuses_endless_pages(armed):
    page = [order_json(intent(sig=f"s{i}").client_id) for i in range(WP.PER_PAGE)]
    t = Scripted(ok({"orders": page}), ok({"orders": page}))
    assert len(broker(t, armed).open_orders()) == WP.PER_PAGE and len(t.calls) == 2
    counter = iter(range(10 ** 6))

    def endless(method, url, headers, body, timeout):
        return ok({"orders": [order_json(intent(sig=f"e{next(counter)}").client_id)
                              for _ in range(WP.PER_PAGE)]})
    with pytest.raises(BrokerDataError, match="pages"):
        broker(endless, armed).open_orders()


def test_open_orders_skips_only_unparseable_orders_that_are_not_ours(armed, caplog):
    foreign = order_json(FOREIGN_CID, type_="OCO")
    rows = [foreign, order_json(CID)]
    orders = broker(Scripted(ok({"orders": rows})), armed).open_orders()
    assert [o.client_id for o in orders] == [CID]
    assert "did not place" in caplog.text
    for bad_rows in ([order_json(CID, type_="OCO")], [{"symbol": SYM, "status": "NEW"}]):
        with pytest.raises(BrokerDataError):
            broker(Scripted(ok({"orders": bad_rows})), armed).open_orders()


def test_balances_and_fee_requests_and_parsing(armed):
    balances = {"balances": {
        "USDT": {"asset": "USDT", "faName": "tether", "fiat": False,
                 "value": "1250.5000000000000000", "locked": "250.5000000000000000"},
        "BTC": {"asset": "BTC", "faName": "bitcoin", "fiat": False,
                "value": "0.0100000000000000", "locked": "0"}}}
    fees = {"BTCUSDT": {"makerFeeRate": "0.0020000000", "takerFeeRate": "0.0025000000",
                        "recent_days_sum": 0},
            "ETHUSDT": {"makerFeeRate": "0.0030", "takerFeeRate": "0.0035", "recent_days_sum": "1"}}
    t = Scripted(ok(balances), ok(fees), ok(fees), ok(fees))
    b = broker(t, armed)
    assert b.balances() == {"USDT": Balance(D("1000"), D("250.5")), "BTC": Balance(D("0.01"), D(0))}
    assert b.fee_rates() == FeeRates(D("0.003"), D("0.0035"))           # highest of any market
    assert b.fee_rates(SYM) == FeeRates(D("0.002"), D("0.0025"))
    with pytest.raises(BrokerDataError):
        b.fee_rates("XRPUSDT")
    assert [urlsplit(c.url).path for c in t.calls] == \
        ["/v1/account/balances"] + ["/v1/account/fee"] * 3
    assert {c.method for c in t.calls} == {"GET"}
    assert {c.headers["X-API-Key"] for c in t.calls} == {FAKE_READ_KEY}


def test_quote_uses_public_depth_without_any_key(armed):
    depth = {"ask": [{"price": "60010.00", "quantity": "1.5", "sum": "90015"}],
             "bid": [{"price": "60000.00", "quantity": "2", "sum": "120000"}]}
    t = Scripted(ok(depth))
    q = broker(t, armed, clock=lambda: 1_760_000_000.0).quote(SYM)
    assert (q.symbol, q.bid, q.ask, q.ts) == (SYM, D("60000.00"), D("60010.00"), 1_760_000_000.0)
    c = t.calls[0]
    assert urlsplit(c.url).path == "/v1/depth" and "X-API-Key" not in c.headers


# ----------------------------------------------------------------------------- response parsing
def partial_order(**extra):
    return order_json(status="PARTIALLY_FILLED", fills=[("60010", "0.003", "0.0000105", "BTC"),
                                                         ("60005", "0.002", "0.000007", "BTC")],
                      **extra)


def test_partial_fill_totals_are_cumulative_over_fills():
    o = parse_order(partial_order(), now=1_760_000_100.0)
    assert o.filled_qty == D("0.005") and o.fee == D("0.0000175") and o.fee_asset == "BTC"
    assert o.filled_quote == D("60010") * D("0.003") + D("60005") * D("0.002")
    assert (o.status, o.quantity, o.price, o.side, o.type) == \
        (OrderStatus.PARTIALLY_FILLED, D("0.01"), D("60010"), Side.BUY, OrderType.LIMIT)
    assert (o.created_ts, o.updated_ts) == (1_760_000_000.0, 1_760_000_100.0)
    assert str(o.filled_qty) == "0.005"                            # canonical, no padding zeros


def test_filled_market_sell_with_fee_in_quote_asset():
    fee = D("61200") * D("0.009965") * D("0.0035")
    o = parse_order(order_json(side="SELL", type_="MARKET", price=None, qty="0.009965",
                               status="FILLED", fills=[("61200", "0.009965", fee, "USDT")]), now=0.0)
    assert (o.side, o.price, o.fee_asset, o.fee) == (Side.SELL, None, "USDT", fee)
    assert o.filled_qty == o.quantity == D("0.009965") and o.status is OrderStatus.FILLED


def test_order_without_fills_has_zero_totals_and_ms_timestamps_are_understood():
    o = parse_order(order_json(transactTime=1_760_000_000_123), now=5.0)
    assert (o.filled_qty, o.filled_quote, o.fee, o.fee_asset) == (0, 0, 0, "")
    assert o.created_ts == pytest.approx(1_760_000_000.123)
    assert parse_order(order_json(transactTime="garbage"), now=5.0).created_ts == 5.0


def _drop(key):
    return lambda o: o.pop(key)


def _set(key, value):
    return lambda o: o.__setitem__(key, value)


def _fill(i, key, value=None, drop=False):
    def mutate(o):
        if drop:
            del o["fills"][i][key]
        else:
            o["fills"][i][key] = value
    return mutate


MUTATIONS = {
    "missing clientOrderId": _drop("clientOrderId"), "missing symbol": _drop("symbol"),
    "missing side": _drop("side"), "missing type": _drop("type"), "missing origQty": _drop("origQty"),
    "missing status": _drop("status"), "missing fills": _drop("fills"),
    "missing price on LIMIT": _drop("price"), "null origQty": _set("origQty", None),
    "non-numeric origQty": _set("origQty", "abc"), "float origQty": _set("origQty", 0.01),
    "bool price": _set("price", True), "negative price": _set("price", "-60010"),
    "exponent string": _set("origQty", "1E-2"), "padded number": _set("origQty", " 0.01"),
    "NaN string": _set("price", "NaN"), "zero origQty": _set("origQty", "0"),
    "unknown side": _set("side", "LONG"), "unknown type": _set("type", "OCO"),
    "status not a string": _set("status", 1), "fills not a list": _set("fills", {}),
    "bad clientOrderId": _set("clientOrderId", "wb_<script>"), "bad symbol": _set("symbol", "btc/usdt"),
    "fill missing fee": _fill(0, "fee", drop=True), "fill missing feeAsset": _fill(0, "feeAsset", drop=True),
    "fill missing price": _fill(0, "price", drop=True),
    "fill non-numeric quantity": _fill(1, "quantity", "two"), "fill zero quantity": _fill(1, "quantity", "0"),
    "fill negative fee": _fill(0, "fee", "-0.1"), "fill bad feeAsset": _fill(0, "feeAsset", "BTC!"),
    "mixed fee assets": _fill(1, "feeAsset", "USDT"), "fill for another symbol": _fill(0, "symbol", "ETHUSDT"),
    "fills exceed origQty": _set("origQty", w16("0.004")),
    "FILLED but fills incomplete": _set("status", "FILLED"),
    "executedQty disagrees with fills": _set("executedQty", w16("0.006")),
    "huge number": _set("origQty", "1" * 31),
}


@pytest.mark.parametrize("label", sorted(MUTATIONS))
def test_mutated_or_missing_order_fields_fail_closed(label):
    o = partial_order()
    MUTATIONS[label](o)
    with pytest.raises(BrokerDataError) as err:
        parse_order(o, now=0.0)
    assert isinstance(err.value, WallexDataError) and isinstance(err.value, BrokerError)


@pytest.mark.parametrize("body", [
    b"", b"not json", b"[1, 2]", b'{"success": true}', b'{"success": "true", "result": {}}',
    b'{"success": false, "result": {}}', b'{"success": true, "result": null}',
    b'{"success": true, "result": NaN}', b'{"success": true, "success": true, "result": {}}',
    b"\xff\xfe{}", b"[" * 100_000 + b"]" * 100_000,
])
def test_unusable_response_envelopes_fail_closed(armed, body):
    with pytest.raises(BrokerDataError):
        broker(Scripted(raw(200, body)), armed).get_order(CID)


def test_get_order_refuses_another_orders_data(armed):
    with pytest.raises(BrokerDataError, match="not the requested"):
        broker(Scripted(ok(order_json(OTHER_CID))), armed).get_order(CID)


STATUS_CASES = [
    ("NEW", (), OrderStatus.NEW), ("new", (), OrderStatus.NEW),
    ("PARTIALLY_FILLED", (("60010", "0.004", "0.000014", "BTC"),), OrderStatus.PARTIALLY_FILLED),
    ("FILLED", (("60010", "0.01", "0.000035", "BTC"),), OrderStatus.FILLED),
    ("CANCELED", (), OrderStatus.CANCELED), ("CANCELLED", (("60010", "0.004", "0.000014", "BTC"),),
                                             OrderStatus.CANCELED),
    ("EXPIRED", (), OrderStatus.CANCELED), ("REJECTED", (), OrderStatus.REJECTED),
]
UNKNOWN_CASES = [
    ("PENDING_CANCEL", (), OrderStatus.NEW),
    ("PENDING_CANCEL", (("60010", "0.004", "0.000014", "BTC"),), OrderStatus.PARTIALLY_FILLED),
    ("SOMETHING_NEW", (), OrderStatus.NEW), ("", (), OrderStatus.NEW),
]


@pytest.mark.parametrize("raw_status, fills, expected", STATUS_CASES + UNKNOWN_CASES)
def test_status_mapping_table_and_conservative_unknowns(raw_status, fills, expected, caplog):
    caplog.set_level(logging.WARNING, logger="wallexbot")
    o = parse_order(order_json(status=raw_status, fills=fills), now=0.0)
    assert o.status is expected
    known = raw_status.upper() in WP.STATUS_MAP
    assert ("unknown order status" in caplog.text) is not known
    assert set(WP.STATUS_MAP) == {"NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "CANCELLED",
                                  "EXPIRED", "REJECTED"}


BALANCE_MUTATIONS = [
    lambda r: r["balances"]["USDT"].pop("value"), lambda r: r["balances"]["USDT"].pop("locked"),
    lambda r: r["balances"]["USDT"].__setitem__("locked", "2000"),
    lambda r: r["balances"]["USDT"].__setitem__("value", "lots"),
    lambda r: r["balances"]["USDT"].__setitem__("asset", "BTC"),
    lambda r: r.__setitem__("balances", [{"asset": "USDT"}]),
    lambda r: r["balances"].__setitem__("US DT", {"value": "1", "locked": "0"}),
    lambda r: r.pop("balances"),
]


@pytest.mark.parametrize("i", range(len(BALANCE_MUTATIONS)))
def test_bad_balances_fail_closed(i):
    result = {"balances": {"USDT": {"asset": "USDT", "value": "1000", "locked": "0"}}}
    BALANCE_MUTATIONS[i](result)
    with pytest.raises(BrokerDataError):
        parse_balances(result)


@pytest.mark.parametrize("result", [
    {}, [], {"BTCUSDT": {"makerFeeRate": "0.25", "takerFeeRate": "0.25"}},     # a percent, not a fraction
    {"BTCUSDT": {"makerFeeRate": "0.002"}}, {"BTCUSDT": "0.002"},
    {"BTCUSDT": {"makerFeeRate": "x", "takerFeeRate": "0.002"}},
    {"BTC/USDT": {"makerFeeRate": "0.002", "takerFeeRate": "0.002"}},
])
def test_bad_fee_tables_fail_closed(result):
    with pytest.raises(BrokerDataError):
        parse_fee_table(result)


# ----------------------------------------------------------------------------- POST error semantics
POST_AMBIGUOUS = {
    "timeout": TimeoutError("The read operation timed out"),
    "connection reset": ConnectionResetError("Connection reset by peer"),
    "url error": urllib.error.URLError("timed out"),
    "remote disconnected": http.client.RemoteDisconnected("Remote end closed connection"),
    "incomplete read": http.client.IncompleteRead(b"{\"res"),
    "refused redirect": UrlNotAllowed("only api.wallex.ir is allowed"),
    "anything else": RuntimeError("transport bug"),
    "500": raw(500), "502 html": raw(502, b"<html>502 Bad Gateway</html>"), "503": raw(503),
    "504": raw(504), "408": raw(408), "202": raw(202), "204": raw(204), "302": raw(302),
    "307": raw(307), "101": raw(101),
    "201 not json": raw(201, b"<html>created</html>"),
    "201 success false": raw(201, envelope(order_json(), success=False)),
    "200 null result": raw(200, b'{"success": true, "result": null}'),
    "201 another client_id": ok(order_json(OTHER_CID), 201),
    "201 another quantity": ok(order_json(qty="0.02"), 201),
    "201 another side": ok(order_json(side="SELL"), 201),
    "201 malformed order": ok({"clientOrderId": CID}, 201),
    "status not an int": ("201", {}, envelope(order_json())),
}


@pytest.mark.parametrize("label", sorted(POST_AMBIGUOUS))
def test_ambiguous_post_outcomes_raise_broker_timeout_after_exactly_one_call(armed, label):
    t = Scripted(POST_AMBIGUOUS[label])
    with pytest.raises(BrokerTimeout, match="reconcile by client_id"):
        place(broker(t, armed, max_retries=5))
    assert len(t.calls) == 1                                     # a POST is never retried


POST_DEFINITE = {
    "429": (fail(429, "Too Many Attempts."), RateLimited),
    "401": (fail(401, "Unauthenticated."), AuthRejected),
    "403": (fail(403, "This API key has no trade permission."), AuthRejected),
    "403 html": (raw(403, b"<html>403 Forbidden</html>"), AuthRejected),
    "409": (fail(409, "Conflict"), DuplicateClientId),
    "400 already taken": (fail(400, "The client id has already been taken."), DuplicateClientId),
    "422 client_id error": (fail(422, "The given data was invalid.", client_id=["invalid"]),
                            DuplicateClientId),
    "400 persian duplicate": (fail(400, "سفارش تکراری"),
                              DuplicateClientId),
    "400 insufficient": (fail(400, "Insufficient balance."), OrderRejected),
    "422 price": (fail(422, "The given data was invalid.", price=["The price is too low."]),
                  OrderRejected),
    "404": (fail(404, "Not Found"), OrderRejected),
    "405 empty": (raw(405), OrderRejected),
}


@pytest.mark.parametrize("label", sorted(POST_DEFINITE))
def test_definite_post_outcomes_map_to_the_oms_contract(armed, label):
    response, error = POST_DEFINITE[label]
    t = Scripted(response)
    with pytest.raises(error) as err:
        place(broker(t, armed))
    assert len(t.calls) == 1
    assert type(err.value) is error


def test_auth_rejected_is_distinct_and_never_a_definite_rejection():
    assert issubclass(AuthRejected, BrokerError)
    assert not issubclass(AuthRejected, (OrderRejected, RateLimited, BrokerTimeout, AuthError))


def test_local_rate_limit_refuses_before_any_io(armed):
    now = [0.0]
    bucket = TokenBucket(2, 10.0, clock=lambda: now[0])
    t = Scripted(ok(order_json(CID), 201), ok(order_json(OTHER_CID), 201))
    b = broker(t, armed, rate_limiter=bucket)
    place(b, CID)
    place(b, OTHER_CID)
    with pytest.raises(RateLimited, match="nothing was sent"):
        place(b, intent(sig="sig-3").client_id)
    assert len(t.calls) == 2
    default = broker(Scripted(), armed)._limiter
    assert (default.capacity, default.rate) == (10, 1.0)        # 10 creates per 10 s


# ----------------------------------------------------------------------------- GET / DELETE semantics
def test_get_retries_are_bounded_with_backoff(armed):
    slept = []
    t = Scripted(TimeoutError("t"), raw(502), raw(503))
    with pytest.raises(BrokerTimeout, match="3 attempts"):
        broker(t, armed, sleep=slept.append, max_retries=2, backoff_s=0.5).balances()
    assert len(t.calls) == 3 and slept == [0.5, 1.0]


@pytest.mark.parametrize("transient", [ConnectionResetError("reset"), raw(500), raw(408),
                                       http.client.RemoteDisconnected("closed")])
def test_get_recovers_after_a_transient_failure(armed, transient):
    t = Scripted(transient, ok({"balances": {"USDT": {"value": "5", "locked": "0"}}}))
    assert broker(t, armed).balances() == {"USDT": Balance(D("5"), D(0))} and len(t.calls) == 2


@pytest.mark.parametrize("response, error", [
    (fail(401, "Unauthenticated."), AuthRejected), (fail(403, "IP not allowed."), AuthRejected),
    (fail(429, "Too Many Attempts."), RateLimited), (fail(400, "Bad request"), BrokerHTTPError),
    (raw(404, b"<html>Not Found</html>"), BrokerHTTPError),
    (ValueError("transport bug"), BrokerError),
    (UrlNotAllowed("redirect to another host refused"), UrlNotAllowed),   # a security stop
])
def test_get_does_not_retry_definite_errors(armed, response, error):
    t = Scripted(response)
    with pytest.raises(error):
        broker(t, armed, max_retries=5).balances()
    assert len(t.calls) == 1


def test_get_order_is_none_only_for_a_json_not_found(armed):
    assert broker(Scripted(fail(404, "Order not found.")), armed).get_order(CID) is None
    for response in (raw(404, b"<html>Not Found</html>"), raw(404, b'{"message": "Not Found"}'),
                     raw(404, b""), fail(400, "Order not found."), fail(422, "Order not found.")):
        with pytest.raises(BrokerHTTPError):
            broker(Scripted(response), armed).get_order(CID)


@pytest.mark.parametrize("bad", ["../balances", f"{CID}/../x", CID.upper(), "", None, 42,
                                 "wb_" + "0" * 31])
def test_client_ids_are_validated_before_any_io(armed, bad):
    t = Scripted()
    b = broker(t, armed)
    with pytest.raises(ValueError):
        b.get_order(bad)
    with pytest.raises(ValueError):
        b.cancel_order(bad)
    assert t.calls == []


def test_delete_retries_on_5xx_then_reads_back(armed):
    t = Scripted(raw(503), raw(500), ok({}), ok(order_json(status="CANCELED")))
    order = broker(t, armed, max_retries=2).cancel_order(CID)
    assert [c.method for c in t.calls] == ["DELETE", "DELETE", "DELETE", "GET"]
    assert order.status is OrderStatus.CANCELED
    t = Scripted(raw(503), raw(503), raw(503))
    with pytest.raises(BrokerTimeout):
        broker(t, armed, max_retries=2).cancel_order(CID)
    assert len(t.calls) == 3


# ----------------------------------------------------------------------------- arming and keys
def test_submit_and_cancel_without_arming_send_nothing():
    t = Scripted()
    b = WallexBroker(Secret(FAKE_READ_KEY), Secret(FAKE_TRADE_KEY), transport=t)
    assert b.is_armed is False
    with pytest.raises(AuthError, match="not armed"):
        place(b)
    with pytest.raises(AuthError, match="not armed"):
        b.cancel_order(CID)
    assert t.calls == []


def test_an_equal_secret_is_not_arming_evidence(armed):
    forged = Secret(FAKE_TRADE_KEY)
    assert forged == armed and is_live_armed_token(armed) and not is_live_armed_token(forged)
    assert not is_live_armed_token(FAKE_TRADE_KEY) and not is_live_armed_token(None)
    t = Scripted()
    with pytest.raises(AuthError, match="live_armed"):
        WallexBroker(Secret(FAKE_READ_KEY), forged, transport=t, live_armed=forged)
    assert t.calls == []


def test_live_armed_must_belong_to_the_trade_key(armed):
    other = Secret("FAKE_other_trade_key_TEST_Zz99Yy88")
    for trade in (other, None):
        with pytest.raises(AuthError):
            WallexBroker(Secret(FAKE_READ_KEY), trade, transport=Scripted(), live_armed=armed)
    b = WallexBroker(Secret(FAKE_READ_KEY), Secret(FAKE_TRADE_KEY), transport=Scripted(),
                     live_armed=armed)
    assert b.is_armed                      # the same key value loaded separately is fine


def test_arming_failures_mint_no_token():
    before = len(A._ARMED_TOKENS)
    for env, flag in ((ARMED_ENV, False), ({"WALLEX_API_KEY_TRADE": FAKE_TRADE_KEY}, True),
                      ({"WALLEX_ARM_LIVE": "YES_I_UNDERSTAND"}, True)):
        with pytest.raises(A.LiveNotArmed):
            assert_live_armed(dict(env), flag)
    assert len(A._ARMED_TOKENS) == before


def test_read_key_cannot_place_or_cancel_orders(armed):
    t = Scripted()
    b = WallexBroker(Secret(FAKE_READ_KEY), None, transport=t)
    with pytest.raises(AuthError, match="no trade key"):
        place(b)
    with pytest.raises(AuthError, match="no trade key"):
        b.cancel_order(CID)
    with pytest.raises(AuthError):
        WallexBroker(Secret(FAKE_READ_KEY), None, transport=t, live_armed=armed)
    # arming with the READ key's value as the trade key is refused too (two equal keys)
    read_as_trade = assert_live_armed({**ARMED_ENV, "WALLEX_API_KEY_TRADE": FAKE_READ_KEY}, True)
    with pytest.raises(AuthError, match="same key"):
        WallexBroker(Secret(FAKE_READ_KEY), read_as_trade, transport=t, live_armed=read_as_trade)
    assert t.calls == []
    assert not hasattr(WallexAccountReader, "submit_order")
    assert not hasattr(WallexAccountReader, "cancel_order")


def test_orders_carry_only_the_trade_key_and_reads_only_the_read_key(armed):
    t = Scripted(ok(order_json(), 201), ok(order_json()), ok({}), ok(order_json(status="CANCELED")))
    b = broker(t, armed)
    place(b)
    b.get_order(CID)
    b.cancel_order(CID)
    assert [(c.method, c.headers["X-API-Key"]) for c in t.calls] == [
        ("POST", FAKE_TRADE_KEY), ("GET", FAKE_READ_KEY), ("DELETE", FAKE_TRADE_KEY),
        ("GET", FAKE_READ_KEY)]


def test_keys_must_be_secrets_and_well_formed():
    with pytest.raises(TypeError):
        WallexBroker(FAKE_READ_KEY, None)
    with pytest.raises(TypeError):
        WallexBroker(None, FAKE_TRADE_KEY)
    with pytest.raises(TypeError):
        WallexAccountReader(FAKE_READ_KEY)
    with pytest.raises(AuthError):
        WallexBroker(None, None)
    with pytest.raises(AuthError):
        WallexBroker(Secret("FAKE key with spaces TEST"), None)
    with pytest.raises(AuthError):
        WallexBroker(Secret(FAKE_READ_KEY), None, sub_account="FAKE sub\r\nX: 1")
    with pytest.raises(TypeError):
        WallexBroker(Secret(FAKE_READ_KEY), None, allow_stop_orders="yes")
    with pytest.raises(ValueError):
        WallexBroker(Secret(FAKE_READ_KEY), None, max_retries=50)


def test_default_transport_is_the_pinned_safe_opener(monkeypatch):
    assert WallexBroker(Secret(FAKE_READ_KEY), None)._transport is WP.urllib_transport
    seen = []

    class Response:
        status = 201
        headers = email.message.Message()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return b'{"ok": 1}'

    class Opener:
        def __init__(self, outcome):
            self.outcome = outcome

        def open(self, req, timeout):
            seen.append((req.get_method(), req.full_url, req.get_header("X-api-key"), timeout))
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    monkeypatch.setattr(WP, "safe_opener", lambda: Opener(Response()))
    assert WP.urllib_transport("POST", f"{BASE}/v1/account/orders", {"X-API-Key": FAKE_TRADE_KEY},
                               b"{}", 5.0)[0::2] == (201, b'{"ok": 1}')
    err = urllib.error.HTTPError(f"{BASE}/v1/account/balances", 503, "Busy", email.message.Message(),
                                 io.BytesIO(b"busy"))
    monkeypatch.setattr(WP, "safe_opener", lambda: Opener(err))
    assert WP.urllib_transport("GET", f"{BASE}/v1/account/balances", {}, None, 5.0)[0::2] == \
        (503, b"busy")
    assert seen[0] == ("POST", f"{BASE}/v1/account/orders", FAKE_TRADE_KEY, 5.0)
    with pytest.raises(UrlNotAllowed):                         # checked before any socket is opened
        WP.urllib_transport("GET", "https://evil.example.com/v1", {}, None, 5.0)
    assert len(seen) == 2


def test_broker_satisfies_the_broker_protocol():
    for name in ("submit_order", "cancel_order", "get_order", "open_orders", "balances",
                 "fee_rates", "quote"):
        proto = inspect.signature(getattr(Broker, name)).parameters
        impl = inspect.signature(getattr(WallexBroker, name)).parameters
        for p in proto.values():
            assert p.name in impl and impl[p.name].kind == p.kind, (name, p.name)


def test_client_id_pattern_matches_the_store():
    assert WP._CLIENT_ID_RE.pattern == CLIENT_ID_RE.pattern


# ----------------------------------------------------------------------------- secrets
def test_no_secret_in_exception_text_repr_or_logs(armed, caplog):
    caplog.set_level(logging.DEBUG)
    sub = Secret(FAKE_SUB)
    echo = f"bad key {FAKE_TRADE_KEY} / {FAKE_READ_KEY} (sub-account {FAKE_SUB})"
    texts: list[str] = []

    def run(action, *responses):
        t = Scripted(*responses)
        b = broker(t, armed, sub_account=sub, max_retries=1)
        texts.extend([repr(b), str(b), repr(b._reader)])
        try:
            action(b)
        except Exception as e:  # every failure path: its text must be clean
            texts.extend([str(e), repr(e), repr(e.args), repr(e.__cause__), repr(e.__context__)])
        texts.extend(c.headers_repr for c in t.calls)

    get = lambda b: b.get_order(CID)                       # noqa: E731
    cancel = lambda b: b.cancel_order(CID)                 # noqa: E731
    for status in (400, 401, 403, 404, 409, 422, 429):
        run(place, fail(status, echo))
        run(get, fail(status, echo))
        run(cancel, fail(status, echo), fail(status, echo))
    for exc in (OSError(echo), TimeoutError(echo), RuntimeError(echo), ValueError(echo),
                http.client.IncompleteRead(echo.encode()), UrlNotAllowed("refused")):
        run(place, exc)
        run(get, exc, exc)
        run(cancel, exc, exc)
        run(lambda b: b.quote(SYM), exc, exc, exc, exc)
    run(place, raw(500, echo.encode()))
    run(place, raw(400, echo.encode()))
    run(place, raw(201, envelope({"clientOrderId": echo})))
    run(place, ok(order_json(status=echo), 201))            # unknown status: logged
    run(get, ok(order_json(status=echo, symbol=echo)))
    run(lambda b: b.balances(), ok({"balances": {echo: {"value": "1", "locked": "0"}}}))
    run(lambda b: b.open_orders(), ok({"orders": [order_json("foreign-1", type_=echo)]}))
    run(lambda b: b.fee_rates(), ok({"BTCUSDT": {"makerFeeRate": echo, "takerFeeRate": "0"}}))
    text = "\n".join(texts + [caplog.text] + [r.getMessage() for r in caplog.records])
    assert "Secret(****" in text and "rejected" in text      # the paths really ran
    assert scan_text_for_secrets(text, [FAKE_TRADE_KEY, FAKE_READ_KEY, FAKE_SUB],
                                 heuristics=False) == []


# ----------------------------------------------------------------------------- no fund movement
def test_private_module_names_no_fund_movement_endpoint_and_only_known_paths():
    source = Path(WP.__file__).read_text()
    for word in ("withdraw", "iban", "transfer"):
        assert word not in source.lower(), word
    constants = {n.value for n in ast.walk(ast.parse(source))
                 if isinstance(n, ast.Constant) and isinstance(n.value, str) and "/v1" in n.value}
    assert constants == {"/v1/account/orders", "/v1/account/openOrders", "/v1/account/balances",
                         "/v1/account/fee"} == set(WP.ENDPOINT_PATHS)


@pytest.mark.parametrize("path", ["/v1/account/withdraw", "/v1/account/crypto-withdrawal",
                                  "/v1/account/money-withdrawal", "/v1/account/%77ithdraw",
                                  "/v1/WITHDRAW/request", "/v1/account/orders/../withdraw"])
def test_withdraw_paths_are_refused_before_any_io(path):
    with pytest.raises(UrlNotAllowed):
        assert_host_allowed(BASE + path)
    t = Scripted()
    reader = WallexAccountReader(Secret(FAKE_READ_KEY), transport=t)
    with pytest.raises(UrlNotAllowed):
        reader._get(path)
    assert t.calls == []


def test_reader_class_only_ever_sends_get():
    tree = ast.parse(Path(WP.__file__).read_text())
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "WallexAccountReader")
    methods = {n.value for n in ast.walk(cls) if isinstance(n, ast.Constant)
               and n.value in ("GET", "POST", "DELETE", "PUT", "PATCH")}
    assert methods == {"GET"}


# ----------------------------------------------------------------------------- OMS integration
class FakeWallex:
    """Behaves like the ASSUMED Wallex private API (plus public depth) behind the transport
    interface: client_id is unique (a second create is refused with 409 and creates nothing),
    fills[] are cumulative, unknown orders are 404 + success:false, BUY fees are paid in the base
    asset and SELL fees in the quote asset, and only the trade key may create or cancel."""

    FILL_PART = {"fill": D(1), "partial": D("0.5"), "rest": D(0), "timeout_after_accept": D(1),
                 "502_after_accept": D(1)}

    def __init__(self, clock, *, trade_key=FAKE_TRADE_KEY):
        self.clock, self.trade_key = clock, trade_key
        self.rate = D("0.0035")
        self.bid, self.ask = D("60000.00"), D("60010.00")
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.create_modes: deque[str] = deque()

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        parts = urlsplit(url)
        assert (parts.scheme, parts.netloc) == ("https", "api.wallex.ir"), url
        path, query = parts.path, parse_qs(parts.query)
        if path == "/v1/depth":
            assert "X-API-Key" not in headers
            return ok({"ask": [{"price": str(self.ask), "quantity": "5"}],
                       "bid": [{"price": str(self.bid), "quantity": "5"}]})
        key = headers.get("X-API-Key")
        if key not in (FAKE_READ_KEY, self.trade_key):
            return fail(401, "Unauthenticated.")
        if method == "POST" and path == "/v1/account/orders":
            if key != self.trade_key:
                return fail(403, "This API key has no trade permission.")
            return self.create(json.loads(body))
        if path.startswith("/v1/account/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            if method == "GET":
                return ok(order) if order else fail(404, "Order not found.")
            if method == "DELETE":
                if key != self.trade_key:
                    return fail(403, "This API key has no trade permission.")
                if order is None:
                    return fail(404, "Order not found.")
                if order["status"] not in ("NEW", "PARTIALLY_FILLED"):
                    return fail(422, "Order is not open.")
                order.update(status="CANCELED", active=False)
                return ok(order)
        if method == "GET" and path == "/v1/account/openOrders":
            page, per = int(query["page"][0]), int(query["per_page"][0])
            rows = [o for o in self.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED")]
            return ok({"orders": rows[(page - 1) * per: page * per]})
        if method == "GET" and path == "/v1/account/balances":
            return ok({"balances": {"USDT": {"asset": "USDT", "value": w16(10000), "locked": "0"},
                                    "BTC": {"asset": "BTC", "value": "0", "locked": "0"}}})
        return raw(404, b"<html>404 Not Found</html>")

    def create(self, req):
        mode = self.create_modes.popleft() if self.create_modes else "fill"
        if mode == "timeout_before_accept":
            raise TimeoutError("The write operation timed out")
        cid = req["client_id"]
        if cid in self.orders:
            return fail(409, "Duplicate client_id.")
        self.orders[cid] = order_json(cid, symbol=req["symbol"], side=req["side"],
                                      type_=req["type"], qty=req["quantity"],
                                      price=req.get("price"), transactTime=int(self.clock()))
        part = D(req["quantity"]) * self.FILL_PART[mode]
        if part:
            self.fill(cid, part)
        if mode == "timeout_after_accept":
            raise TimeoutError("The read operation timed out")
        if mode == "502_after_accept":
            return raw(502, b"<html>502 Bad Gateway</html>")
        return ok(self.orders[cid], status=201)

    def fill(self, cid, qty):
        o = self.orders[cid]
        buy = o["side"] == "BUY"
        price = D(o["price"]) if o["type"] == "LIMIT" else (self.ask if buy else self.bid)
        fee, asset = (qty * self.rate, "BTC") if buy else (price * qty * self.rate, "USDT")
        o["fills"].append(fill_json(price, qty, fee, asset, o["symbol"]))
        executed = D(o["executedQty"]) + qty
        o.update(executedQty=w16(executed),
                 executedSum=w16(D(o["executedSum"]) + price * qty),
                 status="FILLED" if executed == D(o["origQty"]) else "PARTIALLY_FILLED")
        o["active"] = o["status"] != "FILLED"


class LiveRig:
    def __init__(self, cfg, rules, clock, store, armed, *, arm=True, fake_trade_key=FAKE_TRADE_KEY):
        self.clock, self.store = clock, store
        self.fake = FakeWallex(clock, trade_key=fake_trade_key)
        trade = armed if arm else Secret(FAKE_TRADE_KEY)
        self.broker = WallexBroker(Secret(FAKE_READ_KEY), trade, transport=self.fake, clock=clock,
                                   rate_limiter=TokenBucket(10, 10.0, clock=clock),
                                   live_armed=armed if arm else None, sleep=lambda s: None)
        self.ks = KillSwitch(store)
        self.risk = RiskEngine(cfg, rules, self.ks)
        self.oms = OMS(store, self.broker, self.risk, self.ks, approve=auto_approve, clock=clock,
                       not_found_grace_s=30)

    def submit(self, i):
        quote = self.broker.quote(i.symbol)
        account = snapshot_account(self.store, self.broker.balances(), "USDT", {SYM: quote},
                                   self.clock())
        return self.oms.submit(i, quote, account)

    def db_text(self):
        return Path(self.store.path).read_bytes().decode("latin-1") + "".join(
            p.read_bytes().decode("latin-1") for p in Path(self.store.path).parent.glob("*-wal"))


@pytest.fixture
def live(cfg, rules, clock, store, armed):
    return LiveRig(cfg, rules, clock, store, armed)


def test_oms_timeout_after_accept_never_duplicates_and_reconciles_via_get_order(live):
    live.fake.create_modes.append("timeout_after_accept")
    i = intent()
    res = live.submit(i)
    assert res.sent and res.state is S.UNKNOWN_PENDING
    assert len(live.fake.posts()) == 1 and len(live.fake.orders) == 1    # created at the exchange
    assert live.oms.positions() == {}                                     # nothing booked yet
    for _ in range(3):                                                    # never POSTed again
        assert live.submit(i).sent is False
    assert len(live.fake.posts()) == 1
    assert live.oms.reconcile() == {i.client_id: S.FILLED}
    assert any(m == "GET" and u.endswith(f"/v1/account/orders/{i.client_id}")
               for m, u, _h, _b in live.fake.calls)
    assert len(live.fake.posts()) == 1 and len(live.fake.orders) == 1
    assert live.oms.positions()[SYM].quantity == D("0.009965")           # 0.01 - 0.000035 BTC fee
    assert len(live.store.rows("fills")) == 1
    assert live.oms.reconcile() == {}


def test_oms_5xx_after_accept_is_ambiguous_then_reconciled(live):
    live.fake.create_modes.append("502_after_accept")
    i = intent()
    assert live.submit(i).state is S.UNKNOWN_PENDING
    assert live.oms.reconcile() == {i.client_id: S.FILLED} and len(live.fake.posts()) == 1


def test_oms_timeout_before_accept_resolves_to_rejected_after_grace(live):
    live.fake.create_modes.append("timeout_before_accept")
    i = intent()
    assert live.submit(i).state is S.UNKNOWN_PENDING
    assert live.oms.reconcile() == {i.client_id: S.UNKNOWN_PENDING}       # within grace: wait
    live.clock.advance(31)
    assert live.oms.reconcile() == {i.client_id: S.REJECTED}
    assert len(live.fake.posts()) == 1 and live.fake.orders == {}
    assert live.submit(i).sent is False


def test_oms_duplicate_client_id_adopts_the_existing_order(live):
    i = intent()
    live.fake.create({"client_id": i.client_id, "symbol": SYM, "side": "BUY", "type": "LIMIT",
                      "quantity": "0.01", "price": "60010"})      # placed before a local DB loss
    res = live.submit(i)
    assert res.state is S.UNKNOWN_PENDING and len(live.fake.orders) == 1
    assert live.oms.reconcile() == {i.client_id: S.FILLED}
    assert live.oms.positions()[SYM].quantity == D("0.009965") and len(live.fake.posts()) == 1


def test_oms_partial_fill_then_rest_via_reconcile(live):
    live.fake.create_modes.append("partial")
    i = intent()
    assert live.submit(i).state is S.PARTIALLY_FILLED
    assert live.oms.reconcile() == {i.client_id: S.PARTIALLY_FILLED}
    assert len(live.store.rows("fills")) == 1
    live.fake.fill(i.client_id, D("0.005"))
    assert live.oms.reconcile() == {i.client_id: S.FILLED}
    assert [D(f["quantity"]) for f in live.store.rows("fills")] == [D("0.005"), D("0.005")]
    assert live.oms.positions()[SYM].quantity == D("0.009965")


def test_oms_cancel_stale_goes_through_delete_and_read_back(live):
    live.fake.create_modes.append("rest")
    i = intent(price="59000.00")
    assert live.submit(i).state is S.SUBMITTED
    live.clock.advance(301)
    assert live.oms.cancel_stale(300) == [i.client_id]
    assert live.store.get_intent(i.client_id).state is S.CANCELED
    assert [c[0] for c in live.fake.calls if "/v1/account/orders/" in c[1]] == ["DELETE", "GET"]


def test_oms_with_an_unarmed_broker_never_posts(cfg, rules, clock, store, armed):
    rig = LiveRig(cfg, rules, clock, store, armed, arm=False)
    i = intent()
    res = rig.submit(i)
    assert res.state is S.UNKNOWN_PENDING and rig.fake.posts() == []      # refused before any I/O
    rig.clock.advance(31)
    assert rig.oms.reconcile() == {i.client_id: S.REJECTED}
    assert rig.fake.posts() == [] and rig.fake.orders == {}


def test_oms_auth_rejected_post_is_reconciled_never_resent(cfg, rules, clock, store, armed):
    rig = LiveRig(cfg, rules, clock, store, armed, fake_trade_key="FAKE_rotated_key_TEST_000000")
    i = intent()
    assert rig.submit(i).state is S.UNKNOWN_PENDING                       # 401 -> ambiguous
    rig.clock.advance(31)
    assert rig.oms.reconcile() == {i.client_id: S.REJECTED}               # read key: not found
    assert len(rig.fake.posts()) == 1 and rig.submit(i).sent is False


def test_audit_db_holds_no_key_material_after_live_flows(live):
    live.fake.create_modes.extend(["timeout_after_accept", "partial"])
    assert live.submit(intent()).state is S.UNKNOWN_PENDING
    assert live.oms.reconcile() == {CID: S.FILLED}
    second = intent(sig="sig-2", price="59990.00")
    assert live.submit(second).state is S.PARTIALLY_FILLED
    live.fake.create_modes.append("rest")
    assert len(live.fake.posts()) == 2
    assert scan_text_for_secrets(live.db_text(), [FAKE_READ_KEY, FAKE_TRADE_KEY],
                                 heuristics=False) == []
