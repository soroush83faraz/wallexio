"""ShadowBroker: simulated execution against the LIVE public order book. It never sends anything.

Purpose: shadow-mode runs. The OMS / risk / audit path is exercised for real, but every "exchange"
call is answered from memory, using depth snapshots from `book_source` (production wiring passes
`WallexPublicClient.depth`; tests pass fakes). This module imports no networking code at all; the
only I/O it can do is appending to the optional local JSONL journal.

Fill model (all assumptions are conservative; where Wallex docs are silent they are marked):

- Timing. An accepted order is NEW (or PENDING_TRIGGER for stops) and becomes *eligible* at
  `created_ts + latency_s`. With latency_s == 0 it is matched at once against the snapshot taken at
  submit time. Otherwise it is matched by `step(now)` against a FRESH snapshot taken once it is
  eligible, so adverse moves during the latency window are felt. Reads (get_order, open_orders,
  balances) never touch the book or change state; the bot loop must call `step()` regularly.
- Marketable LIMIT (first eligible look): walks the ACTUAL levels from the best price while the
  level price is inside the limit, as TAKER (taker_rate). Each level is one fill, so VWAP is
  filled_quote / filled_qty. If the visible depth inside the limit is too thin the order is
  PARTIALLY_FILLED and the remainder rests.
- Resting LIMIT, maker rule: a resting BUY at L fills only on a LATER snapshot (strictly greater
  `ts` than the one it last looked at) and only against levels that trade THROUGH the limit by at
  least one tick (best ask <= L - tick, tick = 10**-price_precision). It fills at the limit price L,
  as MAKER (maker_rate), for at most the visible quantity at those through-levels. Mirror for SELL
  (best bid >= L + tick). Touching the limit exactly never fills (queue position is unknown, so no
  queue-jumping). Displayed liquidity is consumed within one `step()` / submit so two orders cannot
  fill against the same visible quantity in the same snapshot; across snapshots depth is treated as
  a price feed (a persistent level can fill successive orders). [assumption]
- MARKET (and triggered STOP_MARKET) walks the book as taker and is limited to `market_band_pct`
  (default 2%) from the best price and to visible depth: the unfilled remainder is auto-canceled
  (status CANCELED, possibly with a partial fill), as MockWallex does. A plain MARKET order whose
  mid moved more than the band between decision (submit) time and its eligible snapshot is
  auto-canceled with no fill. [assumption: Wallex "last-price move > 2%" is approximated by mid]
- STOP_LIMIT / STOP_MARKET: PENDING_TRIGGER, funds locked, out of the book until the MID of a
  snapshot taken after eligibility crosses stop_price (BUY: mid >= stop, SELL: mid <= stop). On
  trigger it is processed in that same snapshot: STOP_MARKET as MARKET, STOP_LIMIT as a LIMIT.
- `slippage_bps_extra`: extra pessimism applied against every TAKER fill (BUY pays more, SELL
  receives less). A LIMIT order is never filled worse than its limit; maker fills are untouched.
- Fees: BUY fills pay the fee in the base asset, SELL fills pay it in the quote asset (same
  documented assumption as MockWallex). Funds are locked on accept (stops included) and released on
  cancel / completion.
- client_id must be unique: a second submit raises DuplicateClientId and creates nothing.
- Deterministic given the sequence of calls, snapshots and clock values. No threads.

Journal (JSONL, one object per line, Decimals as strings): events accept, reject, duplicate, fill,
trigger, cancel, book_error. `shadow_report(path)` summarises it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from wallexbot.exchange.base import (OPEN_STATUSES, Balance, BrokerOrder, DuplicateClientId, FeeRates,
                                     OrderRejected, OrderStatus)
from wallexbot.types import OrderType, Quote, Side, SymbolRules, round_down

_ZERO = Decimal(0)
_BPS = Decimal(10_000)
_STOPS = (OrderType.STOP_LIMIT, OrderType.STOP_MARKET)


class BookSnapshot(Protocol):
    """Structural twin of exchange.wallex_public.Depth (kept separate so this module imports no I/O)."""
    asks: tuple[tuple[Decimal, Decimal], ...]   # (price, quantity), best (lowest) first
    bids: tuple[tuple[Decimal, Decimal], ...]   # (price, quantity), best (highest) first
    ts: float


BookSource = Callable[[str], BookSnapshot]


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"not serialisable: {type(o).__name__}")


def _mid(book: BookSnapshot) -> Decimal | None:
    if not book.asks or not book.bids:
        return None
    return (book.asks[0][0] + book.bids[0][0]) / 2


@dataclass
class _Order:
    seq: int
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
    eligible_ts: float
    decision_mid: Decimal
    filled_qty: Decimal = _ZERO
    filled_quote: Decimal = _ZERO
    fee: Decimal = _ZERO
    triggered: bool = False
    rest_ts: float | None = None  # ts of the snapshot at which the order started/last looked while resting

    def snapshot(self) -> BrokerOrder:
        return BrokerOrder(self.client_id, self.symbol, self.side, self.type, self.quantity, self.price,
                           self.stop_price, self.status, self.filled_qty, self.filled_quote, self.fee,
                           self.fee_asset, self.created_ts, self.updated_ts)

    @property
    def is_market(self) -> bool:
        return self.type is OrderType.MARKET or (self.type is OrderType.STOP_MARKET and self.triggered)


class ShadowBroker:
    def __init__(self, book_source: BookSource, rules: Iterable[SymbolRules],
                 starting_balances: dict[str, Decimal], maker_rate: Decimal, taker_rate: Decimal,
                 clock: Callable[[], float], latency_s: float = 1.0, *,
                 slippage_bps_extra: Decimal | int | str = 0, market_band_pct: Decimal = Decimal("2"),
                 journal_path: str | Path | None = None) -> None:
        if latency_s < 0:
            raise ValueError("latency_s must be >= 0")
        self.book_source = book_source
        self.rules = {r.symbol: r for r in rules}
        self.maker_rate, self.taker_rate = Decimal(maker_rate), Decimal(taker_rate)
        self.clock = clock
        self.latency_s = float(latency_s)
        self.slip = Decimal(str(slippage_bps_extra)) / _BPS
        if self.slip < 0:
            raise ValueError("slippage_bps_extra must be >= 0")
        self.band = Decimal(market_band_pct) / 100
        self.journal_path = None if journal_path is None else Path(journal_path)
        if self.journal_path is not None:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._free: dict[str, Decimal] = defaultdict(Decimal, {k: Decimal(v) for k, v in starting_balances.items()})
        self._locked: dict[str, Decimal] = defaultdict(Decimal)
        self.orders: dict[str, _Order] = {}
        self.submit_calls = 0
        self.events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ Broker protocol (reads)
    def quote(self, symbol: str) -> Quote:
        book = self.book_source(symbol)
        if not book.asks or not book.bids:
            raise OrderRejected(f"empty book for {symbol}")
        return Quote(symbol, book.bids[0][0], book.asks[0][0], book.ts)

    def fee_rates(self) -> FeeRates:
        return FeeRates(self.maker_rate, self.taker_rate)

    def balances(self) -> dict[str, Balance]:
        assets = set(self._free) | set(self._locked)
        return {a: Balance(self._free[a], self._locked[a]) for a in sorted(assets)}

    def get_order(self, client_id: str) -> BrokerOrder | None:
        o = self.orders.get(client_id)
        return None if o is None else o.snapshot()

    def open_orders(self, symbol: str | None = None) -> list[BrokerOrder]:
        return [o.snapshot() for o in self.orders.values()
                if o.status in OPEN_STATUSES and (symbol is None or o.symbol == symbol)]

    # ------------------------------------------------------------------ Broker protocol (writes)
    def cancel_order(self, client_id: str) -> BrokerOrder | None:
        o = self.orders.get(client_id)
        if o is None:
            return None
        if o.status in OPEN_STATUSES:  # a terminal order (e.g. already FILLED) is returned unchanged
            now = self.clock()
            o.status = OrderStatus.CANCELED
            self._release(o, now)
            self._emit("cancel", now, client_id=client_id, reason="requested", filled_qty=o.filled_qty)
        return o.snapshot()

    def submit_order(self, *, client_id: str, symbol: str, side: Side, type: OrderType,
                     quantity: Decimal, price: Decimal | None = None,
                     stop_price: Decimal | None = None) -> BrokerOrder:
        """Accept an order into the SIMULATION. Nothing leaves the machine."""
        self.submit_calls += 1
        now = self.clock()
        if client_id in self.orders:
            self._emit("duplicate", now, client_id=client_id)
            raise DuplicateClientId(client_id)
        try:
            rules = self._validate(symbol, side, type, quantity, price, stop_price)
            book = self._fetch(symbol)
            mid = _mid(book) if book is not None else None
            if mid is None:
                raise OrderRejected("no market data")
            lock_asset, lock_amt = self._lock_need(rules, side, type, quantity, price, stop_price, book)
            if rules.min_notional is not None:
                ref = price if price is not None else (stop_price if stop_price is not None else mid)
                if quantity * ref < rules.min_notional:
                    raise OrderRejected("below min notional")
            if self._free[lock_asset] < lock_amt:
                raise OrderRejected("insufficient balance")
        except OrderRejected as e:
            self._emit("reject", now, client_id=client_id, symbol=symbol, side=side, type=type,
                       quantity=quantity, price=price, stop_price=stop_price, reason=str(e))
            raise
        self._free[lock_asset] -= lock_amt
        self._locked[lock_asset] += lock_amt
        stop = type in _STOPS
        o = _Order(len(self.orders), client_id, symbol, side, type, quantity, price, stop_price,
                   OrderStatus.PENDING_TRIGGER if stop else OrderStatus.NEW,
                   fee_asset=rules.base if side is Side.BUY else rules.quote, lock_asset=lock_asset,
                   locked=lock_amt, created_ts=now, updated_ts=now, eligible_ts=now + self.latency_s,
                   decision_mid=mid)
        self.orders[client_id] = o
        self._emit("accept", now, client_id=client_id, symbol=symbol, side=side, type=type,
                   quantity=quantity, price=price, stop_price=stop_price, mid_at_decision=mid,
                   eligible_ts=o.eligible_ts, book_ts=book.ts)
        if self.latency_s == 0:
            self._advance(o, book, now, defaultdict(Decimal))
        return o.snapshot()

    # ------------------------------------------------------------------ simulation clock
    def step(self, now: float | None = None) -> list[BrokerOrder]:
        """Advance every eligible open order against a fresh book. Returns the orders that changed."""
        now = float(self.clock()) if now is None else float(now)
        due = sorted((o for o in self.orders.values()
                      if o.status in OPEN_STATUSES and now >= o.eligible_ts),
                     key=lambda o: (o.created_ts, o.seq))
        books: dict[str, BookSnapshot | None] = {}
        used: dict[tuple, Decimal] = defaultdict(Decimal)
        changed = []
        for o in due:
            if o.symbol not in books:
                books[o.symbol] = self._fetch(o.symbol, now)
            book = books[o.symbol]
            if book is None:
                continue
            before = (o.status, o.filled_qty)
            self._advance(o, book, now, used)
            if (o.status, o.filled_qty) != before:
                changed.append(o.snapshot())
        return changed

    # ------------------------------------------------------------------ internals: book + validation
    def _fetch(self, symbol: str, ts: float | None = None) -> BookSnapshot | None:
        try:
            return self.book_source(symbol)
        except Exception as e:  # a failed snapshot must never create or move an order
            self._emit("book_error", self.clock() if ts is None else ts, symbol=symbol, error=repr(e))
            return None

    def _validate(self, symbol, side, type, quantity, price, stop_price) -> SymbolRules:
        rules = self.rules.get(symbol)
        if rules is None:
            raise OrderRejected(f"unknown market {symbol}")
        if quantity <= 0 or round_down(quantity, rules.amount_precision) != quantity:
            raise OrderRejected("invalid amount / amount precision")
        if type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and price is None:
            raise OrderRejected("price required")
        if type in _STOPS and stop_price is None:
            raise OrderRejected("stop_price required")
        for p in (price, stop_price):
            if p is not None and (p <= 0 or round_down(p, rules.price_precision) != p):
                raise OrderRejected("invalid price / price precision")
        return rules

    def _lock_need(self, rules, side, type, quantity, price, stop_price, book) -> tuple[str, Decimal]:
        if side is Side.SELL:
            return rules.base, quantity
        if type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            return rules.quote, quantity * price
        ref = stop_price if type is OrderType.STOP_MARKET else book.asks[0][0]
        return rules.quote, quantity * ref * (1 + self.band) * (1 + self.slip)

    def _release(self, o: _Order, now: float) -> None:
        self._free[o.lock_asset] += o.locked
        self._locked[o.lock_asset] -= o.locked
        o.locked = _ZERO
        o.updated_ts = now

    # ------------------------------------------------------------------ internals: matching
    def _advance(self, o: _Order, book: BookSnapshot, now: float, used: dict) -> None:
        if o.status is OrderStatus.PENDING_TRIGGER:
            mid = _mid(book)
            if mid is None:
                return
            if not (mid >= o.stop_price if o.side is Side.BUY else mid <= o.stop_price):
                return
            o.triggered, o.status, o.rest_ts = True, OrderStatus.NEW, None
            o.updated_ts = now
            self._emit("trigger", now, client_id=o.client_id, mid=mid, stop_price=o.stop_price)
        if o.is_market:
            self._run_market(o, book, now, used)
        else:
            self._run_limit(o, book, now, used)

    def _run_market(self, o: _Order, book: BookSnapshot, now: float, used: dict) -> None:
        levels = book.asks if o.side is Side.BUY else book.bids
        if not levels:
            return self._auto_cancel(o, now, "empty_book")
        if o.type is OrderType.MARKET:  # a triggered stop already moved by design: skip the move check
            mid = _mid(book)
            if mid is not None and abs(mid - o.decision_mid) / o.decision_mid > self.band:
                return self._auto_cancel(o, now, "move_gt_band")
        if o.side is Side.BUY and not self._relock_market_buy(o, levels[0][0], now):
            return self._auto_cancel(o, now, "insufficient_balance")
        best = levels[0][0]
        limit = best * (1 + self.band) if o.side is Side.BUY else best * (1 - self.band)
        self._take(o, book, levels, limit, now, used, cap_at_limit=False)
        if o.filled_qty == o.quantity:
            o.status = OrderStatus.FILLED
            self._release(o, now)
        else:
            self._auto_cancel(o, now, "size_gt_depth_or_band")

    def _run_limit(self, o: _Order, book: BookSnapshot, now: float, used: dict) -> None:
        if o.rest_ts is None:  # first eligible look: marketable part is taken as taker
            levels = book.asks if o.side is Side.BUY else book.bids
            self._take(o, book, levels, o.price, now, used, cap_at_limit=True)
        elif book.ts > o.rest_ts:  # a LATER snapshot: maker fill only on trade-through
            self._maker(o, book, now, used)
        o.rest_ts = book.ts
        if o.filled_qty == o.quantity:
            o.status = OrderStatus.FILLED
            self._release(o, now)
        elif o.filled_qty > 0:
            o.status = OrderStatus.PARTIALLY_FILLED

    def _take(self, o: _Order, book: BookSnapshot, levels, limit: Decimal, now: float, used: dict,
              *, cap_at_limit: bool) -> None:
        side_key = "ask" if o.side is Side.BUY else "bid"
        for px, qty in levels:
            remaining = o.quantity - o.filled_qty
            if remaining <= 0:
                break
            if (o.side is Side.BUY and px > limit) or (o.side is Side.SELL and px < limit):
                break
            key = (o.symbol, side_key, px)
            avail = qty - used[key]
            if avail <= 0:
                continue
            take = min(avail, remaining)
            fill_px = px * (1 + self.slip) if o.side is Side.BUY else px * (1 - self.slip)
            if cap_at_limit:
                fill_px = min(fill_px, limit) if o.side is Side.BUY else max(fill_px, limit)
            used[key] += take
            self._fill(o, take, fill_px, self.taker_rate, "taker", book, now)

    def _maker(self, o: _Order, book: BookSnapshot, now: float, used: dict) -> None:
        tick = Decimal(1).scaleb(-self.rules[o.symbol].price_precision)
        buy = o.side is Side.BUY
        levels = book.asks if buy else book.bids
        side_key = "ask" if buy else "bid"
        for px, qty in levels:
            remaining = o.quantity - o.filled_qty
            if remaining <= 0:
                break
            if (buy and px > o.price - tick) or (not buy and px < o.price + tick):
                break  # not through the limit by a full tick (levels are sorted best first)
            key = (o.symbol, side_key, px)
            avail = qty - used[key]
            if avail <= 0:
                continue
            take = min(avail, remaining)
            used[key] += take
            self._fill(o, take, o.price, self.maker_rate, "maker", book, now)

    def _relock_market_buy(self, o: _Order, best_ask: Decimal, now: float) -> bool:
        need = (o.quantity - o.filled_qty) * best_ask * (1 + self.band) * (1 + self.slip)
        extra = need - o.locked
        if extra > 0:
            if self._free[o.lock_asset] < extra:
                return False
            self._free[o.lock_asset] -= extra
            self._locked[o.lock_asset] += extra
            o.locked += extra
        return True

    def _auto_cancel(self, o: _Order, now: float, reason: str) -> None:
        o.status = OrderStatus.CANCELED
        self._release(o, now)
        self._emit("cancel", now, client_id=o.client_id, reason=reason, filled_qty=o.filled_qty)

    def _fill(self, o: _Order, qty: Decimal, px: Decimal, rate: Decimal, liquidity: str,
              book: BookSnapshot, now: float) -> None:
        rules = self.rules[o.symbol]
        amount = qty * px
        if o.side is Side.BUY:
            fee = qty * rate
            spent = amount
            self._free[rules.base] += qty - fee
            slip_bps = (px - o.decision_mid) / o.decision_mid * _BPS
        else:
            fee = amount * rate
            spent = qty
            self._free[rules.quote] += amount - fee
            slip_bps = (o.decision_mid - px) / o.decision_mid * _BPS
        o.locked -= spent
        self._locked[o.lock_asset] -= spent
        o.filled_qty += qty
        o.filled_quote += amount
        o.fee += fee
        o.updated_ts = now
        self._emit("fill", now, client_id=o.client_id, symbol=o.symbol, side=o.side, type=o.type,
                   quantity=qty, price=px, quote=amount, fee=fee, fee_asset=o.fee_asset,
                   liquidity=liquidity, mid_at_decision=o.decision_mid, slippage_bps=slip_bps,
                   book_ts=book.ts)

    # ------------------------------------------------------------------ journal
    def _emit(self, event: str, ts: float, **fields: Any) -> None:
        rec = {"event": event, "ts": ts, **fields}
        self.events.append(rec)
        if self.journal_path is not None:
            line = json.dumps(rec, default=_json_default, sort_keys=True)
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def shadow_report(journal_path: str | Path) -> dict[str, Any]:
    """Summarise a ShadowBroker JSONL journal.

    Slippage is in basis points versus the mid at decision (submit) time; positive = worse for us.
    Averages are notional-weighted.
    """
    qty: dict[str, Decimal] = {}
    filled: dict[str, Decimal] = defaultdict(Decimal)
    canceled: set[str] = set()
    auto_canceled: set[str] = set()
    rejects: dict[str, int] = defaultdict(int)
    n_dup = n_fills = n_taker = n_maker = n_trigger = 0
    notional = taker_notional = Decimal(0)
    slip_w = taker_slip_w = Decimal(0)
    worst: Decimal | None = None
    fees: dict[str, Decimal] = defaultdict(Decimal)
    for line in Path(journal_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        ev = rec["event"]
        if ev == "accept":
            qty[rec["client_id"]] = Decimal(rec["quantity"])
        elif ev == "reject":
            rejects[rec["reason"]] += 1
        elif ev == "duplicate":
            n_dup += 1
        elif ev == "trigger":
            n_trigger += 1
        elif ev == "cancel":
            canceled.add(rec["client_id"])
            if rec["reason"] != "requested":
                auto_canceled.add(rec["client_id"])
        elif ev == "fill":
            n_fills += 1
            amount, slip = Decimal(rec["quote"]), Decimal(rec["slippage_bps"])
            filled[rec["client_id"]] += Decimal(rec["quantity"])
            fees[rec["fee_asset"]] += Decimal(rec["fee"])
            notional += amount
            slip_w += slip * amount
            worst = slip if worst is None else max(worst, slip)
            if rec["liquidity"] == "taker":
                n_taker += 1
                taker_notional += amount
                taker_slip_w += slip * amount
            else:
                n_maker += 1
    full = sum(1 for c, q in qty.items() if filled[c] >= q)
    partial = sum(1 for c, q in qty.items() if 0 < filled[c] < q)
    return {
        "orders": len(qty),
        "filled_orders": full,
        "partial_orders": partial,
        "unfilled_orders": sum(1 for c in qty if filled[c] == 0),
        "canceled_orders": len(canceled),
        "auto_canceled_orders": len(auto_canceled),
        "stop_triggers": n_trigger,
        "rejects": sum(rejects.values()),
        "reject_reasons": dict(sorted(rejects.items())),
        "duplicate_client_ids": n_dup,
        "fills": n_fills,
        "taker_fills": n_taker,
        "maker_fills": n_maker,
        "filled_notional": str(notional),
        "fees": {a: str(v) for a, v in sorted(fees.items())},
        "avg_slippage_bps": float(slip_w / notional) if notional else None,
        "avg_taker_slippage_bps": float(taker_slip_w / taker_notional) if taker_notional else None,
        "worst_slippage_bps": None if worst is None else float(worst),
    }
