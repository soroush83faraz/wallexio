"""In-memory Wallex simulator for tests and the demo. No network.

Semantics modelled (assumptions where Wallex docs are silent are marked):
- client_id must be unique: a second submit with the same id raises DuplicateClientId and
  creates nothing.
- LIMIT / MARKET / STOP_LIMIT / STOP_MARKET. Stop orders are PENDING_TRIGGER and stay out of
  the book until the price crosses stop_price (BUY: ask >= stop, SELL: bid <= stop).
- MARKET orders fill only within 2% of the best price and only up to visible depth; the
  remainder is auto-canceled (status CANCELED, possibly with a partial fill). [assumption]
- Fees: BUY fills pay fee in the base asset, SELL fills pay fee in the quote asset. [assumption]
- Funds are locked on submit (stop orders included) and released on cancel/completion. [assumption]
- The book is a price feed: fills do not deplete displayed depth.
- Faults (per submit, via fault_mode or queue_fault): fill, partial, rest, reject,
  timeout_after_accept, timeout_before_accept, 429. lookup_failures makes the next N
  get_order/open_orders calls time out.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable

from wallexbot.exchange.base import (OPEN_STATUSES, Balance, BrokerOrder, BrokerTimeout, DuplicateClientId,
                                     FeeRates, OrderRejected, OrderStatus, RateLimited)
from wallexbot.exchange.ratelimit import TokenBucket
from wallexbot.types import OrderType, Quote, Side, SymbolRules, round_down

FAULT_MODES = frozenset({"fill", "partial", "rest", "reject", "timeout_after_accept",
                         "timeout_before_accept", "429"})
_ZERO = Decimal(0)


class MockClock:
    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


@dataclass
class _Order:
    client_id: str
    symbol: str
    side: Side
    type: OrderType
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    status: OrderStatus
    fee_asset: str
    lock_asset: str
    locked: Decimal
    created_ts: float
    updated_ts: float
    filled_qty: Decimal = _ZERO
    filled_quote: Decimal = _ZERO
    fee: Decimal = _ZERO
    triggered: bool = False

    def snapshot(self) -> BrokerOrder:
        return BrokerOrder(self.client_id, self.symbol, self.side, self.type, self.quantity, self.price,
                           self.stop_price, self.status, self.filled_qty, self.filled_quote, self.fee,
                           self.fee_asset, self.created_ts, self.updated_ts)

    @property
    def is_market(self) -> bool:
        return self.type is OrderType.MARKET or (self.type is OrderType.STOP_MARKET and self.triggered)


class MockWallex:
    def __init__(self, rules: Iterable[SymbolRules], *, clock: Callable[[], float] | None = None,
                 balances: dict[str, Decimal] | None = None, maker_rate: Decimal = Decimal("0.0035"),
                 taker_rate: Decimal = Decimal("0.0035"), market_band_pct: Decimal = Decimal("2"),
                 limiter: TokenBucket | None = None) -> None:
        self.clock = clock or MockClock()
        self.rules = {r.symbol: r for r in rules}
        self.maker_rate, self.taker_rate = maker_rate, taker_rate
        self.band = market_band_pct / 100
        self.limiter = limiter or TokenBucket(10, 10.0, clock=self.clock)
        self._free: dict[str, Decimal] = defaultdict(Decimal, balances or {})
        self._locked: dict[str, Decimal] = defaultdict(Decimal)
        self._books: dict[str, tuple[list, list, float]] = {}
        self.orders: dict[str, _Order] = {}
        self.submit_calls = 0
        self.fault_mode = "fill"
        self._faults: deque[str] = deque()
        self.lookup_failures = 0

    # ------------------------------------------------------------------ test controls
    def queue_fault(self, *modes: str) -> None:
        for m in modes:
            if m not in FAULT_MODES:
                raise ValueError(f"unknown fault mode {m!r}")
        self._faults.extend(modes)

    def set_book(self, symbol: str, bids: list[tuple[Decimal, Decimal]],
                 asks: list[tuple[Decimal, Decimal]]) -> None:
        self._books[symbol] = (sorted(bids, key=lambda x: -x[0]), sorted(asks, key=lambda x: x[0]),
                               self.clock())
        self._on_price(symbol)

    def set_price(self, symbol: str, bid: Decimal, ask: Decimal, depth: Decimal = Decimal("1000")) -> None:
        self.set_book(symbol, [(bid, depth)], [(ask, depth)])

    # ------------------------------------------------------------------ Broker protocol
    def quote(self, symbol: str) -> Quote:
        bids, asks, ts = self._books[symbol]
        return Quote(symbol, bids[0][0], asks[0][0], ts)

    def fee_rates(self) -> FeeRates:
        return FeeRates(self.maker_rate, self.taker_rate)

    def balances(self) -> dict[str, Balance]:
        assets = set(self._free) | set(self._locked)
        return {a: Balance(self._free[a], self._locked[a]) for a in sorted(assets)}

    def get_order(self, client_id: str) -> BrokerOrder | None:
        self._maybe_lookup_fault()
        o = self.orders.get(client_id)
        return None if o is None else o.snapshot()

    def open_orders(self, symbol: str | None = None) -> list[BrokerOrder]:
        self._maybe_lookup_fault()
        return [o.snapshot() for o in self.orders.values()
                if o.status in OPEN_STATUSES and (symbol is None or o.symbol == symbol)]

    def cancel_order(self, client_id: str) -> BrokerOrder | None:
        o = self.orders.get(client_id)
        if o is None:
            return None
        if o.status in OPEN_STATUSES:
            o.status = OrderStatus.CANCELED
            self._release(o)
        return o.snapshot()

    def submit_order(self, *, client_id: str, symbol: str, side: Side, type: OrderType,
                     quantity: Decimal, price: Decimal | None = None,
                     stop_price: Decimal | None = None) -> BrokerOrder:
        self.submit_calls += 1
        mode = self._faults.popleft() if self._faults else self.fault_mode
        if mode == "429" or not self.limiter.try_acquire():
            raise RateLimited("429 Too Many Requests")
        if client_id in self.orders:
            raise DuplicateClientId(client_id)
        if mode == "timeout_before_accept":
            raise BrokerTimeout("timeout before the exchange accepted the order")
        if mode == "reject":
            raise OrderRejected("rejected by exchange (fault injection)")
        rules = self._validate(symbol, side, type, quantity, price, stop_price)
        lock_asset, lock_amt = self._lock_need(rules, side, type, quantity, price, stop_price)
        if self._free[lock_asset] < lock_amt:
            raise OrderRejected("insufficient balance")
        self._free[lock_asset] -= lock_amt
        self._locked[lock_asset] += lock_amt
        now = self.clock()
        stop = type in (OrderType.STOP_LIMIT, OrderType.STOP_MARKET)
        o = _Order(client_id, symbol, side, type, quantity, price, stop_price,
                   OrderStatus.PENDING_TRIGGER if stop else OrderStatus.NEW,
                   fee_asset=rules.base if side is Side.BUY else rules.quote,
                   lock_asset=lock_asset, locked=lock_amt, created_ts=now, updated_ts=now)
        self.orders[client_id] = o
        if mode == "partial":
            half = round_down(quantity / 2, rules.amount_precision)
            self._process(o, resting=False, cap=half)
        elif mode != "rest":
            self._process(o, resting=False)
        if mode == "timeout_after_accept":
            raise BrokerTimeout("timeout after the exchange accepted the order")
        return o.snapshot()

    # ------------------------------------------------------------------ internals
    def _maybe_lookup_fault(self) -> None:
        if self.lookup_failures > 0:
            self.lookup_failures -= 1
            raise BrokerTimeout("lookup timeout")

    def _validate(self, symbol, side, type, quantity, price, stop_price) -> SymbolRules:
        rules = self.rules.get(symbol)
        if rules is None:
            raise OrderRejected(f"unknown market {symbol}")
        if quantity <= 0 or round_down(quantity, rules.amount_precision) != quantity:
            raise OrderRejected("invalid amount / amount precision")
        if type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and price is None:
            raise OrderRejected("price required")
        if type in (OrderType.STOP_LIMIT, OrderType.STOP_MARKET) and stop_price is None:
            raise OrderRejected("stop_price required")
        for p in (price, stop_price):
            if p is not None and (p <= 0 or round_down(p, rules.price_precision) != p):
                raise OrderRejected("invalid price / price precision")
        if type is OrderType.MARKET and symbol not in self._books:
            raise OrderRejected("no market data")
        return rules

    def _lock_need(self, rules, side, type, quantity, price, stop_price) -> tuple[str, Decimal]:
        if side is Side.SELL:
            return rules.base, quantity
        if type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            return rules.quote, quantity * price
        ref = stop_price if type is OrderType.STOP_MARKET else self._books[rules.symbol][1][0][0]
        return rules.quote, quantity * ref * (1 + self.band)

    def _release(self, o: _Order) -> None:
        self._free[o.lock_asset] += o.locked
        self._locked[o.lock_asset] -= o.locked
        o.locked = _ZERO
        o.updated_ts = self.clock()

    def _on_price(self, symbol: str) -> None:
        for o in list(self.orders.values()):
            if o.symbol == symbol and o.status in OPEN_STATUSES:
                self._process(o, resting=o.status is not OrderStatus.PENDING_TRIGGER)

    def _process(self, o: _Order, *, resting: bool, cap: Decimal | None = None) -> None:
        if o.symbol not in self._books:
            return
        bids, asks, _ = self._books[o.symbol]
        if o.status is OrderStatus.PENDING_TRIGGER:
            crossed = (asks and asks[0][0] >= o.stop_price) if o.side is Side.BUY \
                else (bids and bids[0][0] <= o.stop_price)
            if not crossed:
                return
            o.triggered, o.status, resting = True, OrderStatus.NEW, False
        if o.is_market and o.side is Side.BUY and asks and not self._relock_market_buy(o, asks[0][0]):
            return
        self._match(o, bids, asks, resting, cap)

    def _relock_market_buy(self, o: _Order, best_ask: Decimal) -> bool:
        need = (o.quantity - o.filled_qty) * best_ask * (1 + self.band)
        extra = need - o.locked
        if extra > 0:
            if self._free[o.lock_asset] < extra:
                o.status = OrderStatus.CANCELED
                self._release(o)
                return False
            self._free[o.lock_asset] -= extra
            self._locked[o.lock_asset] += extra
            o.locked += extra
        return True

    def _match(self, o: _Order, bids, asks, resting: bool, cap: Decimal | None) -> None:
        levels = asks if o.side is Side.BUY else bids
        if not levels:
            return
        target = o.quantity if cap is None else min(o.quantity, cap)
        if o.is_market:
            best = levels[0][0]
            limit = best * (1 + self.band) if o.side is Side.BUY else best * (1 - self.band)
        else:
            limit = o.price
        for px, depth in levels:
            remaining = target - o.filled_qty
            if remaining <= 0:
                break
            if (o.side is Side.BUY and px > limit) or (o.side is Side.SELL and px < limit):
                break
            fill_px = o.price if (resting and not o.is_market) else px
            self._fill(o, min(depth, remaining), fill_px, self.maker_rate if resting else self.taker_rate)
        if o.filled_qty == o.quantity:
            o.status = OrderStatus.FILLED
            self._release(o)
        elif o.is_market and cap is None:
            o.status = OrderStatus.CANCELED  # beyond depth or the 2% band: remainder auto-canceled
            self._release(o)
        elif o.filled_qty > 0:
            o.status = OrderStatus.PARTIALLY_FILLED

    def _fill(self, o: _Order, qty: Decimal, px: Decimal, rate: Decimal) -> None:
        rules = self.rules[o.symbol]
        amount = qty * px
        if o.side is Side.BUY:
            fee = qty * rate
            spent = amount
            self._free[rules.base] += qty - fee
        else:
            fee = amount * rate
            spent = qty
            self._free[rules.quote] += amount - fee
        o.locked -= spent
        self._locked[o.lock_asset] -= spent
        o.filled_qty += qty
        o.filled_quote += amount
        o.fee += fee
        o.updated_ts = self.clock()
