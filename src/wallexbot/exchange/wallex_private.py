"""Wallex PRIVATE API adapter. ``WallexBroker`` implements exchange.base.Broker for the live
account; ``WallexAccountReader`` is its GET-only half (the doctor uses it on its own). Nothing is
sent at import or construction; tests inject a fake transport.

Arming. Live order traffic stays off unless the operator turns it on:
    token = assert_live_armed(os.environ, cli_flag)          # exchange/auth.py; raises unless armed
    broker = WallexBroker(load_secret(READ_KEY_ENV), token, live_armed=token)
submit_order / cancel_order need the trade key AND ``live_armed`` = the very object
assert_live_armed() returned (an identity check via is_live_armed_token, so an equal Secret made
any other way arms nothing). Otherwise they raise AuthError before any I/O. Reads use the READ
key; the trade key is used for reads only when no read key is given. Two equal keys are refused.

Error semantics (the OMS contract in exchange/base.py and oms.py):
  order POST  sent at most once per call and NEVER retried. A transport exception (timeout, reset,
              anything), 408, 5xx, 3xx, an unexpected 2xx, or a 200/201 whose body cannot be used
              => BrokerTimeout (ambiguous: the OMS reconciles by client_id). 429 => RateLimited.
              401/403 => AuthRejected (deliberately not an OrderRejected, so the OMS reconciles it
              too). 409, or any other 4xx whose body mentions a duplicate or the client id
              => DuplicateClientId (ambiguous). Every other 4xx => OrderRejected.
  GET/DELETE  bounded retries with exponential backoff on network errors, 408 and 5xx. 429
              => RateLimited, 401/403 => AuthRejected, other non-2xx => BrokerHTTPError. get_order
              returns None only for a 404 whose body is a JSON ``success: false`` envelope; any
              other 404 (e.g. a router page) raises, because a false "unknown order" would let the
              OMS reject an order that exists.
Every URL passes assert_host_allowed() before it reaches the transport (api.wallex.ir only; the
fund-movement paths are refused there). The key travels only in the X-API-Key header, never in a
URL or body. Every exception message and log line here goes through redact(), and transport
exceptions are summarised, never chained, so their arguments are not carried along.

UNVERIFIED Wallex assumptions (no private request has been made; see WALLEX_INTEGRATION.md):
  * paths: POST ORDERS_PATH, GET and DELETE ORDERS_PATH/{client_id}, GET OPEN_ORDERS_PATH,
    BALANCES_PATH and FEE_PATH (constants below);
  * envelope {"success": true, "result": ...}; errors {"success": false, "message": ...};
  * create body: symbol, side BUY|SELL, type LIMIT|MARKET|STOP_LIMIT|STOP_MARKET, price,
    quantity (decimal strings), client_id, and STOP_PRICE_FIELD for stops (spelling unverified,
    which is why stop orders are refused unless allow_stop_orders=True);
  * order object: clientOrderId, symbol, side, type, origQty, price, status, fills[] of
    {price, quantity, fee, feeAsset}; executedQty (optional cross-check), transactTime (optional);
  * status values: STATUS_MAP below; any other value => a conservative OPEN state, logged;
  * balances: result.balances = {ASSET: {value (TOTAL), locked}}; free = value - locked;
  * fee: result = {MARKET: {makerFeeRate, takerFeeRate}} as fractions (0.0025 = 0.25 %);
  * openOrders: result.orders = [...], paged with PAGE_PARAM / PER_PAGE_PARAM;
  * a 409, or a 4xx body mentioning a duplicate or the client id, means "client_id already used".
"""
from __future__ import annotations

import email.utils
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import timezone
from decimal import (ROUND_HALF_EVEN, Context, Decimal, DecimalException, DivisionByZero, Inexact,
                     InvalidOperation, Overflow, localcontext)

from wallexbot.exchange.auth import (API_HOST, AuthError, assert_host_allowed, build_auth_headers,
                                     is_live_armed_token, safe_opener)
from wallexbot.exchange.base import (OPEN_STATUSES, Balance, BrokerError, BrokerOrder, BrokerTimeout,
                                     DuplicateClientId, FeeRates, OrderRejected, OrderStatus,
                                     RateLimited)
from wallexbot.exchange.ratelimit import TokenBucket
from wallexbot.exchange.wallex_public import WallexDataError, WallexPublicClient
from wallexbot.redact import FAILED, Secret, redact
from wallexbot.types import OrderType, Quote, Side, canonical_decimal

__all__ = ["AuthRejected", "BrokerDataError", "BrokerHTTPError", "STATUS_MAP", "WallexAccountReader",
           "WallexBroker", "parse_balances", "parse_fee_table", "parse_order", "urllib_transport"]

log = logging.getLogger(__name__)

BASE_URL = f"https://{API_HOST}"
ORDERS_PATH = "/v1/account/orders"          # POST: create. GET / DELETE: ORDERS_PATH/{client_id}
OPEN_ORDERS_PATH = "/v1/account/openOrders"
BALANCES_PATH = "/v1/account/balances"
FEE_PATH = "/v1/account/fee"
ENDPOINT_PATHS = frozenset({ORDERS_PATH, OPEN_ORDERS_PATH, BALANCES_PATH, FEE_PATH})
STOP_PRICE_FIELD = "stop_Price"             # documented REQUEST field (create body)
STOP_PRICE_RESPONSE_FIELD = "stopPrice"     # documented RESPONSE field (order object)
PAGE_PARAM, PER_PAGE_PARAM = "page", "per_page"   # UNVERIFIED openOrders paging parameters
PER_PAGE, MAX_PAGES = 100, 20
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_FILLS = 1000
USER_AGENT = "wallexbot/0.1"

# UNVERIFIED: Wallex's order-status enum is not publicly documented. Values not listed here map to
# a conservative OPEN state (NEW, or PARTIALLY_FILLED once fills exist) and are logged, so the OMS
# keeps reconciling instead of finalising an order that may still trade.
STATUS_MAP: Mapping[str, OrderStatus] = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "CANCELLED": OrderStatus.CANCELED,
    "EXPIRED": OrderStatus.CANCELED,       # terminal; any fills are kept
    "REJECTED": OrderStatus.REJECTED,
}
_SIDES = {"BUY": Side.BUY, "SELL": Side.SELL}
_TYPES = {t.value: t for t in OrderType}
_PRICED = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT})
_STOPS = frozenset({OrderType.STOP_LIMIT, OrderType.STOP_MARKET})

_CLIENT_ID_RE = re.compile(r"wb_[0-9a-f]{32}")              # = store.CLIENT_ID_RE (our ids only)
_ANY_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")   # ids in responses (any order)
_SYMBOL_RE = re.compile(r"[A-Z0-9]{3,20}")
_ASSET_RE = re.compile(r"[A-Za-z0-9]{1,20}")
_FEE_KEY_RE = re.compile(r"[A-Za-z0-9_]{1,32}")
_DEC_TEXT = re.compile(r"[0-9]{1,30}(?:\.[0-9]{1,30})?")     # plain decimal: no sign/exponent/space
_EPOCH_TEXT = re.compile(r"[0-9]{1,16}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_DUPLICATE_HINT = re.compile(
    r"duplicat|already\s+(?:been\s+)?(?:exists?|used|taken|registered|submitted|placed)"
    r"|not\s+unique|must\s+be\s+unique|unique\s+constraint"
    r"|client[\s_\-]*(?:order[\s_\-]*)?id|تکراری",  # last: Persian "duplicate"
    re.IGNORECASE)
_MAX_WIRE_DECIMAL = 40
_MAX_FEE_RATE = Decimal("0.05")    # a fee at or above 5 % means the "fraction" assumption is wrong
# Fill sums and products must be exact: a 200-digit context never rounds 30+30-digit inputs, and
# Inexact is trapped anyway, so a surprise becomes a data error instead of a silent residue.
_EXACT = Context(prec=200, rounding=ROUND_HALF_EVEN,
                 traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])

# transport(method, url, headers, body, timeout_s) -> (status, response_headers, body_bytes).
# Network failures raise (OSError / http.client.HTTPException); HTTP errors come back as statuses.
Transport = Callable[[str, str, Mapping[str, str], "bytes | None", float],
                     "tuple[int, Mapping[str, str], bytes]"]


def _m(text: str | BaseException) -> str:
    """Redacted text for an exception message or a log line (never raises). An exception is
    rendered by redact() itself as 'Type: message'."""
    out = redact(text)
    return out if type(out) is str else FAILED


def _clip(text: str, limit: int = 200) -> str:
    text = _CONTROL.sub(" ", text).strip()        # no log-line injection
    return text if len(text) <= limit else text[:limit] + "..."


def _describe(exc: BaseException) -> str:
    """'Type: message', redacted and clipped; redact() never raises, even on hostile objects."""
    return _clip(_m(exc), 160)


# --------------------------------------------------------------------------- errors
class AuthRejected(BrokerError):
    """HTTP 401/403: the key is invalid, expired or revoked, lacks a permission, or the IP is not
    allow-listed. Alert on it. Not an OrderRejected: on an order POST the OMS treats it as
    ambiguous (UNKNOWN_PENDING) and reconciles; nothing is ever resent."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(_m(message))
        self.status = status


class BrokerDataError(WallexDataError, BrokerError):
    """A response is missing a required field or holds an invalid value: do not use it."""

    def __init__(self, message: str) -> None:
        super().__init__(_m(message))


class BrokerHTTPError(BrokerError):
    """A read or cancel got a non-retryable status (or a cancel was refused while still open)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(_m(message))
        self.status = status


# --------------------------------------------------------------------------- transport
def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, dict[str, str], bytes]:
    """Default transport: exchange.auth.safe_opener() (verified TLS; a redirect is followed only
    to the same pinned host, and urllib never re-sends a POST/DELETE body on a redirect)."""
    assert_host_allowed(url)
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with safe_opener().open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as e:           # a non-2xx status: report it, the caller decides
        try:
            data = e.read(MAX_BODY_BYTES + 1) if e.fp is not None else b""
            got = dict(e.headers.items()) if e.headers is not None else {}
        finally:
            e.close()
        return e.code, got, data


def _url(path: str, params: Mapping[str, object] | None = None) -> str:
    query = "?" + urllib.parse.urlencode(params) if params else ""
    return assert_host_allowed(BASE_URL + path + query)     # raises before any I/O


def _headers(key: Secret, sub_account: str | Secret | None, *, json_body: bool = False) -> dict:
    headers = build_auth_headers(key, sub_account)          # AuthHeaders: its repr is redacted
    headers["Accept"] = "application/json"
    headers["User-Agent"] = USER_AGENT
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _status(status: object, what: str) -> int:
    if type(status) is not int or not 100 <= status <= 599:
        raise BrokerDataError(f"{what}: the transport returned an invalid HTTP status")
    return status


def _bytes(body: object, what: str) -> bytes:
    if type(body) is not bytes:
        raise BrokerDataError(f"{what}: the transport returned a non-bytes body")
    return body


def _send_idempotent(transport: Transport, method: str, url: str, headers: Mapping[str, str], *,
                     timeout: float, retries: int, backoff: float, sleep: Callable[[float], None],
                     what: str) -> tuple[int, Mapping[str, str], bytes]:
    """GET / DELETE only (both safe to repeat): bounded retries with exponential backoff on
    network errors, 408 and 5xx. Returns the first other response; BrokerTimeout when exhausted.
    An AuthError from the transport (refused URL or redirect) stops at once; any other unexpected
    exception becomes a BrokerError with a redacted summary (never retried, never chained)."""
    last = "no attempt"
    for attempt in range(retries + 1):
        if attempt:
            sleep(backoff * 2 ** (attempt - 1))
        failure = unexpected = None
        try:
            status, got, body = transport(method, url, headers, None, timeout)
        except AuthError:
            raise
        except (OSError, http.client.HTTPException) as e:   # URLError and timeouts are OSErrors
            failure = _describe(e)
        except Exception as e:  # a transport bug or odd return value
            unexpected = _describe(e)
        if unexpected is not None:
            raise BrokerError(_m(f"{what}: transport error ({unexpected})"))
        if failure is None:
            status = _status(status, what)
            if status != 408 and not 500 <= status <= 599:
                return status, got, _bytes(body, what)
            failure = f"HTTP {status}"
        last = failure
        log.warning("%s", _m(f"{what}: attempt {attempt + 1}/{retries + 1} failed ({failure})"))
    raise BrokerTimeout(_m(f"{what}: no usable response after {retries + 1} attempts ({last})"))


def _check_tuning(timeout_s: object, max_retries: object, backoff_s: object) -> None:
    def number(x: object) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool)
    if not (number(timeout_s) and 0 < timeout_s <= 120):
        raise ValueError("timeout_s must be a number in (0, 120]")
    if type(max_retries) is not int or not 0 <= max_retries <= 5:
        raise ValueError("max_retries must be an int in [0, 5]")
    if not (number(backoff_s) and 0 <= backoff_s <= 60):
        raise ValueError("backoff_s must be a number in [0, 60]")


# --------------------------------------------------------------------------- response helpers
def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key in a JSON object")    # ambiguous: refuse it
        out[k] = v
    return out


def _no_constant(name: str) -> object:
    raise ValueError(f"JSON constant {name} is not allowed")


def _json(body: bytes, what: str) -> object:
    if len(body) > MAX_BODY_BYTES:
        raise BrokerDataError(f"{what}: response larger than {MAX_BODY_BYTES} bytes")
    try:
        return json.loads(body.decode("utf-8"), parse_float=Decimal, parse_constant=_no_constant,
                          object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as e:     # UnicodeDecodeError, JSONDecodeError, ...
        problem = type(e).__name__
    raise BrokerDataError(f"{what}: response is not valid JSON ({problem})")


def _result(payload: object, what: str) -> object:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BrokerDataError(f"{what}: not a successful Wallex response envelope")
    if payload.get("result") is None:
        raise BrokerDataError(f"{what}: the response has no result")
    return payload["result"]


def _is_failure_envelope(body: bytes) -> bool:
    try:
        payload = _json(body, "not-found body")
    except BrokerDataError:
        return False
    return isinstance(payload, dict) and payload.get("success") is False


def _body_text(body: object, limit: int = 8192) -> str:
    return body[:limit].decode("utf-8", "replace") if type(body) is bytes else ""


def _error_text(body: object) -> str:
    """The raw error body plus, when it is JSON, its decoded form (so a \\u-escaped message
    is searchable as text)."""
    text = _body_text(body)
    try:
        payload = _json(body, "error body") if type(body) is bytes else None
    except BrokerDataError:
        payload = None
    if payload is None:
        return text
    return text + "\n" + json.dumps(payload, ensure_ascii=False, default=str)


def _server_message(body: object) -> str:
    """The server's own error text, clipped and redacted (it may echo request data)."""
    text = None
    if type(body) is bytes:
        try:
            payload = _json(body, "error body")
        except BrokerDataError:
            payload = None
        if isinstance(payload, dict) and type(payload.get("message")) is str:
            text = payload["message"]
    if text is None:
        text = _body_text(body, 400)
    return _clip(_m(text)) or "no message"


def _raise_refusals(status: int, body: object, what: str) -> None:
    """429 => RateLimited; 401/403 => AuthRejected (logged at ERROR for alerting)."""
    if status == 429:
        raise RateLimited(_m(f"{what}: HTTP 429, rate limited by Wallex"))
    if status in (401, 403):
        message = (f"{what}: HTTP {status}, key rejected (expired or revoked, missing permission, "
                   f"or IP not allow-listed): {_server_message(body)}")
        log.error("%s", _m(message))
        raise AuthRejected(message, status)


# --------------------------------------------------------------------------- field parsers
def _req(obj: object, key: str, what: str) -> object:
    if not isinstance(obj, dict) or obj.get(key) is None:
        raise BrokerDataError(f"{what}: missing required field {key!r}")
    return obj[key]


def _match(value: object, pattern: re.Pattern[str], what: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise BrokerDataError(f"{what}: missing or malformed")
    return value


def _choice(value: object, table: Mapping[str, object], what: str) -> object:
    if type(value) is not str or value.upper() not in table:
        raise BrokerDataError(f"{what}: unsupported value {_clip(repr(value), 40)}")
    return table[value.upper()]


def _canon(d: Decimal) -> Decimal:
    """Value-canonical Decimal (no exponent, no trailing zeros): exact, no context involved."""
    return Decimal(canonical_decimal(d))


def _dec(value: object, what: str, *, positive: bool = False) -> Decimal:
    """A non-negative finite Decimal from a decimal string (or an exact JSON number). Fail closed:
    no bool/float/None, no sign, exponent or whitespace in strings, at most 30+30 digits."""
    if type(value) is str:
        if _DEC_TEXT.fullmatch(value) is None:
            raise BrokerDataError(f"{what}: not a plain decimal string ({_clip(repr(value), 40)})")
        d = Decimal(value)
    elif type(value) is int:                       # JSON integer
        if not 0 <= value < 10 ** 30:
            raise BrokerDataError(f"{what}: integer out of range")
        d = Decimal(value)
    elif type(value) is Decimal:                   # JSON number with a fraction (parse_float)
        if not value.is_finite() or value < 0 or value.adjusted() >= 30 \
                or value.as_tuple().exponent < -30:
            raise BrokerDataError(f"{what}: number out of range")
        d = value
    else:
        raise BrokerDataError(f"{what}: not numeric ({type(value).__name__})")
    if positive and d == 0:
        raise BrokerDataError(f"{what}: must be positive")
    return _canon(d)


def _epoch(value: object) -> float | None:
    """Optional exchange timestamp (unix seconds or milliseconds); None if absent or odd."""
    if type(value) is str and _EPOCH_TEXT.fullmatch(value):
        value = int(value)
    if type(value) is not int or value <= 0:
        return None
    ts = value / 1000.0 if value > 10 ** 11 else float(value)
    return ts if 10 ** 9 <= ts <= 10 ** 11 else None


def _map_status(raw: str, filled_qty: Decimal, what: str) -> OrderStatus:
    mapped = STATUS_MAP.get(raw.upper())
    if mapped is None:
        mapped = OrderStatus.PARTIALLY_FILLED if filled_qty > 0 else OrderStatus.NEW
        log.warning("%s", _m(f"{what}: unknown order status {_clip(repr(raw), 40)}; treated as "
                             f"{mapped.value} (open) so reconciliation continues"))
    return mapped


def parse_order(obj: object, *, now: float, expect_client_id: str | None = None) -> BrokerOrder:
    """One Wallex order object -> BrokerOrder with CUMULATIVE fill totals summed over fills[]."""
    cid = _match(_req(obj, "clientOrderId", "order response"), _ANY_CLIENT_ID_RE,
                 "order response clientOrderId")
    what = f"order {cid}"
    if expect_client_id is not None and cid != expect_client_id:
        raise BrokerDataError(f"{what}: clientOrderId is not the requested client_id")
    symbol = _match(_req(obj, "symbol", what), _SYMBOL_RE, f"{what} symbol")
    side = _choice(_req(obj, "side", what), _SIDES, f"{what} side")
    otype = _choice(_req(obj, "type", what), _TYPES, f"{what} type")
    quantity = _dec(_req(obj, "origQty", what), f"{what} origQty", positive=True)
    price = _dec(_req(obj, "price", what), f"{what} price", positive=True) if otype in _PRICED \
        else None
    stop_raw = obj.get(STOP_PRICE_RESPONSE_FIELD) if otype in _STOPS else None
    stop = None if stop_raw is None else _dec(stop_raw, f"{what} stop price", positive=True)
    raw_status = _req(obj, "status", what)
    if type(raw_status) is not str:
        raise BrokerDataError(f"{what}: status is not a string")
    fills = _req(obj, "fills", what)
    if not isinstance(fills, list) or len(fills) > MAX_FILLS:
        raise BrokerDataError(f"{what}: fills is not a list of at most {MAX_FILLS} entries")

    filled_qty = filled_quote = fee = Decimal(0)
    fee_asset = ""
    try:
        with localcontext(_EXACT):
            for i, f in enumerate(fills):
                fw = f"{what} fill #{i}"
                px = _dec(_req(f, "price", fw), f"{fw} price", positive=True)
                qty = _dec(_req(f, "quantity", fw), f"{fw} quantity", positive=True)
                fee_i = _dec(_req(f, "fee", fw), f"{fw} fee")
                asset = _match(_req(f, "feeAsset", fw), _ASSET_RE, f"{fw} feeAsset")
                if fee_asset and asset != fee_asset:
                    raise BrokerDataError(f"{what}: fills report different fee assets")
                if f.get("symbol") is not None and f["symbol"] != symbol:
                    raise BrokerDataError(f"{fw}: symbol differs from the order's")
                fee_asset = asset
                filled_qty += qty
                filled_quote += px * qty
                fee += fee_i
    except DecimalException:
        raise BrokerDataError(f"{what}: fill arithmetic is not exact") from None
    if filled_qty > quantity:
        raise BrokerDataError(f"{what}: fills add up to more than origQty")
    if obj.get("executedQty") is not None \
            and _dec(obj["executedQty"], f"{what} executedQty") != filled_qty:
        raise BrokerDataError(f"{what}: executedQty differs from the sum of fills (fills incomplete)")
    status = _map_status(raw_status, filled_qty, what)
    if status is OrderStatus.FILLED and filled_qty != quantity:
        raise BrokerDataError(f"{what}: FILLED but the fills do not add up to origQty (incomplete)")
    created = _epoch(obj.get("transactTime"))
    return BrokerOrder(cid, symbol, side, otype, quantity, price, stop, status, _canon(filled_qty),
                       _canon(filled_quote), _canon(fee), fee_asset,
                       created if created is not None else float(now), float(now))


def parse_balances(result: object) -> dict[str, Balance]:
    """result.balances = {ASSET: {value, locked}}; ``value`` is taken as the TOTAL (the
    conservative reading: free = value - locked can only under-state what is spendable)."""
    rows = _req(result, "balances", "balances")
    if not isinstance(rows, dict):
        raise BrokerDataError("balances: 'balances' is not an object")
    out: dict[str, Balance] = {}
    for asset, row in rows.items():
        name = _match(asset, _ASSET_RE, "balances asset name")
        what = f"balance {name}"
        if not isinstance(row, dict):
            raise BrokerDataError(f"{what}: not an object")
        if row.get("asset") is not None and row["asset"] != name:
            raise BrokerDataError(f"{what}: its asset field differs from its key")
        total = _dec(_req(row, "value", what), f"{what} value")
        locked = _dec(_req(row, "locked", what), f"{what} locked")
        if locked > total:
            raise BrokerDataError(f"{what}: locked exceeds value (value is read as the total)")
        with localcontext(_EXACT):
            out[name] = Balance(free=_canon(total - locked), locked=locked)
    return out


def parse_fee_table(result: object) -> dict[str, FeeRates]:
    """result = {MARKET: {makerFeeRate, takerFeeRate}} as fractions; >= 5 % fails closed."""
    if not isinstance(result, dict) or not result:
        raise BrokerDataError("fee: result is not a non-empty object")
    out: dict[str, FeeRates] = {}
    for market, row in result.items():
        name = _match(market, _FEE_KEY_RE, "fee market name")
        what = f"fee {name}"
        rates = []
        for field in ("makerFeeRate", "takerFeeRate"):
            rate = _dec(_req(row, field, what), f"{what} {field}")
            if rate >= _MAX_FEE_RATE:
                raise BrokerDataError(f"{what} {field}: {rate} is not a plausible fraction (is it "
                                      "a percent?)")
            rates.append(rate)
        out[name] = FeeRates(maker=rates[0], taker=rates[1])
    return out


def _our_client_id(value: object, error: type[Exception] = ValueError) -> str:
    if type(value) is not str or _CLIENT_ID_RE.fullmatch(value) is None:
        raise error("client_id must be wb_ + 32 lowercase hex (OrderIntent.client_id)")
    return value


def _symbol(value: object, error: type[Exception] = ValueError) -> str:
    if type(value) is not str or _SYMBOL_RE.fullmatch(value) is None:
        raise error("symbol must be 3-20 chars of A-Z0-9")
    return value


def _wire(value: object, what: str) -> str:
    """A positive Decimal as a plain decimal string for the request body."""
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise OrderRejected(f"{what} must be a positive finite Decimal; nothing was sent")
    text = canonical_decimal(value)
    if len(text) > _MAX_WIRE_DECIMAL:
        raise OrderRejected(f"{what} has too many digits; nothing was sent")
    return text


def _check_echo(order: BrokerOrder, sent: Mapping[str, str]) -> None:
    if (order.symbol, order.side.value, order.type.value, order.quantity) != \
            (sent["symbol"], sent["side"], sent["type"], Decimal(sent["quantity"])):
        raise BrokerDataError("the created order does not match the request "
                              "(symbol/side/type/quantity)")


# --------------------------------------------------------------------------- reader (GET only)
class WallexAccountReader:
    """GET-only client for the private READ endpoints, with exactly one key. It has no code path
    that sends anything but GET; WallexBroker delegates its reads to it and the doctor uses it
    on its own. ``last_server_date`` is (server Date header, local clock) of the last 2xx."""

    def __init__(self, key: Secret, *, sub_account: str | Secret | None = None,
                 transport: Transport | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, timeout_s: float = 10.0,
                 max_retries: int = 2, backoff_s: float = 0.5) -> None:
        if type(key) is not Secret:
            raise TypeError("key must be a Secret, never a raw str")
        build_auth_headers(key, sub_account)          # refuse a malformed header value now
        _check_tuning(timeout_s, max_retries, backoff_s)
        self._key = key
        self._sub_account = sub_account
        self._transport = urllib_transport if transport is None else transport
        self._clock, self._sleep = clock, sleep
        self._timeout, self._retries, self._backoff = float(timeout_s), max_retries, float(backoff_s)
        self.last_server_date: tuple[float, float] | None = None

    def __repr__(self) -> str:
        return f"WallexAccountReader(key={self._key!r})"

    def _get(self, path: str, params: Mapping[str, object] | None = None, *,
             not_found_ok: bool = False) -> object:
        """The ``result`` of a successful GET; None for a JSON not-found when ``not_found_ok``."""
        what = f"GET {path}"
        status, got, body = _send_idempotent(
            self._transport, "GET", _url(path, params), _headers(self._key, self._sub_account),
            timeout=self._timeout, retries=self._retries, backoff=self._backoff,
            sleep=self._sleep, what=what)
        _raise_refusals(status, body, what)
        if status == 404 and not_found_ok and _is_failure_envelope(body):
            return None
        if not 200 <= status <= 299:
            raise BrokerHTTPError(f"{what}: HTTP {status}: {_server_message(body)}", status)
        self._note_date(got)
        return _result(_json(body, what), what)

    def _note_date(self, headers: object) -> None:
        if not isinstance(headers, Mapping):
            return
        for name, value in headers.items():
            if type(name) is str and name.lower() == "date" and type(value) is str:
                try:
                    when = email.utils.parsedate_to_datetime(value)
                except (TypeError, ValueError, IndexError, OverflowError):
                    return
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                self.last_server_date = (when.timestamp(), float(self._clock()))
                return

    def get_order(self, client_id: str) -> BrokerOrder | None:
        """GET ORDERS_PATH/{client_id}; None only for a 404 with a JSON success:false body."""
        cid = _our_client_id(client_id)
        result = self._get(f"{ORDERS_PATH}/{cid}", not_found_ok=True)
        return None if result is None else parse_order(result, now=self._clock(),
                                                        expect_client_id=cid)

    def open_orders(self, symbol: str | None = None) -> list[BrokerOrder]:
        """Every page of GET OPEN_ORDERS_PATH. An order that is not ours (client id not in our
        format) and cannot be parsed is skipped with a warning; any other parse failure, or more
        than MAX_PAGES pages, fails the whole call (a partial list could hide one of ours)."""
        params: dict[str, object] = {} if symbol is None else {"symbol": _symbol(symbol)}
        found: dict[str, BrokerOrder] = {}
        ids: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            result = self._get(OPEN_ORDERS_PATH, {**params, PAGE_PARAM: page, PER_PAGE_PARAM: PER_PAGE})
            rows = _req(result, "orders", "openOrders")
            if not isinstance(rows, list):
                raise BrokerDataError("openOrders: 'orders' is not a list")
            known = len(ids)
            for row in rows:
                cid = row.get("clientOrderId") if isinstance(row, dict) else None
                if type(cid) is str:
                    ids.add(cid)
                parsed = self._open_row(row, cid)
                if parsed is not None:
                    found[parsed.client_id] = parsed
            if len(rows) < PER_PAGE or len(ids) == known:   # last page, or paging is ignored
                break
        else:
            raise BrokerDataError(f"openOrders: more than {MAX_PAGES} pages; refusing a partial list")
        orders = list(found.values())
        return orders if symbol is None else [o for o in orders if o.symbol == symbol]

    def _open_row(self, row: object, cid: object) -> BrokerOrder | None:
        try:
            return parse_order(row, now=self._clock())
        except BrokerDataError as e:
            if type(cid) is not str or _CLIENT_ID_RE.fullmatch(cid) is not None \
                    or _ANY_CLIENT_ID_RE.fullmatch(cid) is None:
                raise                                       # ours, or not identifiable: fail closed
            problem = str(e)
        log.warning("%s", _m(f"openOrders: skipped an order this bot did not place, which could "
                             f"not be parsed ({problem})"))
        return None

    def balances(self) -> dict[str, Balance]:
        return parse_balances(self._get(BALANCES_PATH))

    def fee_table(self) -> dict[str, FeeRates]:
        return parse_fee_table(self._get(FEE_PATH))

    def fee_rates(self, symbol: str | None = None) -> FeeRates:
        """The market's rates, or (symbol=None) the highest maker and taker rate of any market."""
        table = self.fee_table()
        if symbol is None:
            return FeeRates(maker=max(r.maker for r in table.values()),
                            taker=max(r.taker for r in table.values()))
        rates = table.get(_symbol(symbol))
        if rates is None:
            raise BrokerDataError(f"fee: no entry for {symbol}")
        return rates


# --------------------------------------------------------------------------- broker
class WallexBroker:
    """exchange.base.Broker for the live Wallex account (see the module docstring)."""

    def __init__(self, read_key: Secret | None, trade_key: Secret | None,
                 sub_account: str | Secret | None = None, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time, rate_limiter: TokenBucket | None = None,
                 *, live_armed: Secret | None = None, allow_stop_orders: bool = False,
                 sleep: Callable[[float], None] = time.sleep, timeout_s: float = 10.0,
                 max_retries: int = 2, backoff_s: float = 0.5) -> None:
        for name, key in (("read_key", read_key), ("trade_key", trade_key)):
            if key is not None and type(key) is not Secret:
                raise TypeError(f"{name} must be a Secret or None, never a raw str")
        if read_key is None and trade_key is None:
            raise AuthError("WallexBroker needs a read key or a trade key")
        if read_key is not None and trade_key is not None and read_key == trade_key:
            raise AuthError("the read key and the trade key are the same key: use a Read-only key "
                            "for reads and a separate Read+Trade key for orders")
        if live_armed is not None and not (is_live_armed_token(live_armed)
                                           and trade_key is not None and trade_key == live_armed):
            raise AuthError("live_armed must be the object assert_live_armed() returned for this "
                            "trade key")
        if type(allow_stop_orders) is not bool:
            raise TypeError("allow_stop_orders must be a bool")
        _check_tuning(timeout_s, max_retries, backoff_s)
        if trade_key is not None:
            build_auth_headers(trade_key, sub_account)    # refuse a malformed header value now
        self._read_key, self._trade_key, self._live_armed = read_key, trade_key, live_armed
        self._sub_account = sub_account
        self._transport = urllib_transport if transport is None else transport
        self._clock, self._sleep = clock, sleep
        self._timeout, self._retries, self._backoff = float(timeout_s), max_retries, float(backoff_s)
        self._allow_stop = allow_stop_orders
        self._limiter = TokenBucket(10, 10.0) if rate_limiter is None else rate_limiter
        self._reader = WallexAccountReader(
            read_key if read_key is not None else trade_key, sub_account=sub_account,
            transport=self._transport, clock=clock, sleep=sleep, timeout_s=timeout_s,
            max_retries=max_retries, backoff_s=backoff_s)
        self._public = WallexPublicClient(transport=self._public_transport, clock=clock,
                                          sleep=sleep, timeout_s=timeout_s)

    def __repr__(self) -> str:
        return (f"WallexBroker(read_key={self._read_key!r}, trade_key={self._trade_key!r}, "
                f"armed={self.is_armed}, stop_orders={self._allow_stop})")

    @property
    def is_armed(self) -> bool:
        """True only with a trade key and the matching token from assert_live_armed()."""
        return (self._trade_key is not None and self._live_armed is not None
                and is_live_armed_token(self._live_armed) and self._trade_key == self._live_armed)

    def _trading_key(self, action: str) -> Secret:
        if self._trade_key is None:
            raise AuthError(f"{action} refused: no trade key (the read key never places or "
                            "cancels orders); nothing was sent")
        if not self.is_armed:
            raise AuthError(f"{action} refused: live trading is not armed (live_armed must be "
                            "the token assert_live_armed() returned); nothing was sent")
        return self._trade_key

    # ------------------------------------------------------------------ orders (trade key)
    def submit_order(self, *, client_id: str, symbol: str, side: Side, type: OrderType,
                     quantity: Decimal, price: Decimal | None = None,
                     stop_price: Decimal | None = None) -> BrokerOrder:
        """POST ORDERS_PATH once; never retried (see the module docstring for the outcomes)."""
        key = self._trading_key("order placement")
        sent = self._order_body(client_id, symbol, side, type, quantity, price, stop_price)
        if not self._limiter.try_acquire():
            raise RateLimited("local order-create budget exhausted; nothing was sent")
        url = _url(ORDERS_PATH)
        data = json.dumps(sent, separators=(",", ":")).encode("ascii")
        headers = _headers(key, self._sub_account, json_body=True)
        what = f"order create {sent['client_id']}"
        failure = None
        try:
            status, _got, raw = self._transport("POST", url, headers, data, self._timeout)
        except Exception as e:  # anything after the request was handed over is ambiguous
            failure = _describe(e)
        if failure is not None:
            message = (f"{what}: outcome unknown ({failure}); the order may exist - reconcile by "
                       "client_id, never resend")
            log.warning("%s", _m(message))
            raise BrokerTimeout(_m(message))
        return self._create_outcome(status, raw, sent, what)

    def _order_body(self, client_id: object, symbol: object, side: object, otype: object,
                    quantity: object, price: object, stop_price: object) -> dict[str, str]:
        """Validated request body; OrderRejected (nothing sent) on any invalid argument."""
        cid = _our_client_id(client_id, OrderRejected)
        sym = _symbol(symbol, OrderRejected)
        if not isinstance(side, Side) or not isinstance(otype, OrderType):
            raise OrderRejected("side and type must be Side and OrderType members; nothing was sent")
        if otype in _STOPS and not self._allow_stop:
            raise OrderRejected("stop orders are disabled: Wallex's stop-order fields are "
                                "unverified (allow_stop_orders=False); nothing was sent")
        body = {"symbol": sym, "side": side.value, "type": otype.value,
                "quantity": _wire(quantity, "quantity"), "client_id": cid}
        if otype in _PRICED:
            if price is None:
                raise OrderRejected(f"{otype.value} needs a price; nothing was sent")
            body["price"] = _wire(price, "price")
        if otype in _STOPS:
            if stop_price is None:
                raise OrderRejected(f"{otype.value} needs a stop price; nothing was sent")
            body[STOP_PRICE_FIELD] = _wire(stop_price, "stop price")
        return body

    def _create_outcome(self, status: object, raw: object, sent: Mapping[str, str],
                        what: str) -> BrokerOrder:
        if type(status) is not int or type(raw) is not bytes:
            raise BrokerTimeout(_m(f"{what}: the transport returned no usable response; the order "
                                   "may exist - reconcile by client_id"))
        if status in (200, 201):
            try:
                order = parse_order(_result(_json(raw, what), what), now=self._clock(),
                                    expect_client_id=sent["client_id"])
                _check_echo(order, sent)
            except BrokerDataError as e:
                problem = str(e)
            else:
                return order
            message = (f"{what}: HTTP {status} but the response is unusable ({problem}); the "
                       "order may exist - reconcile by client_id")
            log.warning("%s", _m(message))
            raise BrokerTimeout(_m(message))
        if status == 429 or status in (401, 403):
            _raise_refusals(status, raw, what)
        if 400 <= status <= 499 and status != 408:
            detail = _server_message(raw)
            if status == 409 or _DUPLICATE_HINT.search(_error_text(raw)):
                raise DuplicateClientId(_m(f"{what}: HTTP {status}, treated as a duplicate "
                                           f"client_id ({detail})"))
            raise OrderRejected(_m(f"{what}: rejected with HTTP {status} ({detail})"))
        message = (f"{what}: outcome unknown (HTTP {status}); the order may exist - reconcile by "
                   "client_id, never resend")
        log.warning("%s", _m(message))
        raise BrokerTimeout(_m(message))

    def cancel_order(self, client_id: str) -> BrokerOrder | None:
        """DELETE ORDERS_PATH/{client_id} (bounded retries), then read the order back. A refused
        cancel (4xx) is fine when the order is unknown or already final; otherwise it raises."""
        key = self._trading_key("order cancel")
        cid = _our_client_id(client_id)
        what = f"order cancel {cid}"
        status, _got, raw = _send_idempotent(
            self._transport, "DELETE", _url(f"{ORDERS_PATH}/{cid}"),
            _headers(key, self._sub_account), timeout=self._timeout, retries=self._retries,
            backoff=self._backoff, sleep=self._sleep, what=what)
        _raise_refusals(status, raw, what)
        if not (200 <= status <= 299 or 400 <= status <= 499):
            raise BrokerHTTPError(f"{what}: unexpected HTTP {status}", status)
        order = self._reader.get_order(cid)
        if 200 <= status <= 299 or order is None or order.status not in OPEN_STATUSES:
            return order
        raise BrokerHTTPError(f"{what}: refused with HTTP {status} ({_server_message(raw)}) and "
                              "the order is still open", status)

    # ------------------------------------------------------------------ reads (read key)
    def get_order(self, client_id: str) -> BrokerOrder | None:
        return self._reader.get_order(client_id)

    def open_orders(self, symbol: str | None = None) -> list[BrokerOrder]:
        return self._reader.open_orders(symbol)

    def balances(self) -> dict[str, Balance]:
        return self._reader.balances()

    def fee_rates(self, symbol: str | None = None) -> FeeRates:
        return self._reader.fee_rates(symbol)

    def quote(self, symbol: str) -> Quote:
        """Best bid/ask from the PUBLIC depth endpoint; no key is sent with it."""
        return self._public.depth(symbol).quote

    def _public_transport(self, method: str, url: str, timeout: float) -> tuple[int, bytes]:
        """Adapter for WallexPublicClient: no key header. A transport failure is re-raised as a
        redacted OSError (which the public client retries, bounded), never chained."""
        problem = None
        try:
            status, _got, body = self._transport(
                method, url, {"Accept": "application/json", "User-Agent": USER_AGENT}, None, timeout)
        except AuthError:
            raise
        except Exception as e:  # network or transport failure
            problem = _describe(e)
        if problem is not None:
            raise OSError(_m(f"public GET failed ({problem})"))
        return status, body
