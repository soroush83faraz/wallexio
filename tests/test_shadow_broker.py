"""ShadowBroker: simulated execution against (fake) live books. No network, no credentials."""
import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest

from wallexbot.exchange import shadow as shadow_mod
from wallexbot.exchange.base import DuplicateClientId, OrderRejected, OrderStatus
from wallexbot.exchange.mock_wallex import MockClock
from wallexbot.exchange.shadow import ShadowBroker, shadow_report
from wallexbot.exchange.wallex_public import Depth
from wallexbot.oms import OMS
from wallexbot.pipeline import auto_approve, snapshot_account
from wallexbot.risk.engine import RiskEngine
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.types import IntentState, OrderIntent, OrderType, Side, SymbolRules

D = Decimal
S = IntentState
SYM = "BTCUSDT"
MAKER, TAKER = D("0.001"), D("0.002")


class FakeBook:
    """book_source stand-in: every set() is a new snapshot with a strictly later ts."""

    def __init__(self, clock=None):
        self.clock = clock or (lambda: 1_760_000_000.0)
        self.books = {}
        self.n = 0
        self.calls = 0

    def set(self, asks, bids, symbol=SYM):
        self.n += 1
        self.books[symbol] = Depth(symbol, tuple((D(p), D(q)) for p, q in asks),
                                   tuple((D(p), D(q)) for p, q in bids), self.clock() + self.n * 1e-3)

    def set_price(self, bid, ask, depth="1000", symbol=SYM):
        self.set([(ask, depth)], [(bid, depth)], symbol)

    def __call__(self, symbol):
        self.calls += 1
        return self.books[symbol]


@pytest.fixture
def sym_rules():
    return [SymbolRules(SYM, "BTC", "USDT", amount_precision=6, price_precision=2),
            SymbolRules("ETHUSDT", "ETH", "USDT", amount_precision=4, price_precision=2)]


@pytest.fixture
def book():
    b = FakeBook()
    b.set([("100", "1"), ("101", "2"), ("102", "5")], [("99", "3"), ("98", "5")])
    return b


@pytest.fixture
def clk():
    return MockClock()


def make(book, sym_rules, clk, *, latency=0.0, usdt="1000", btc="0", **kw):
    return ShadowBroker(book, sym_rules, {"USDT": D(usdt), "BTC": D(btc)}, MAKER, TAKER, clk,
                        latency_s=latency, **kw)


def send(b, cid, side=Side.BUY, type=OrderType.LIMIT, qty="1", price=None, stop=None):
    return b.submit_order(client_id=cid, symbol=SYM, side=side, type=type, quantity=D(qty),
                          price=None if price is None else D(price),
                          stop_price=None if stop is None else D(stop))


# ----------------------------------------------------------------------------- taker fills
def test_crossing_limit_walks_book_exact_vwap_and_fee(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    o = send(b, "a", qty="1.5", price="101")
    # 1 @ 100 + 0.5 @ 101 = 150.5 ; VWAP 150.5/1.5 ; taker fee in BASE = 1.5 * 0.002
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == D("1.5") and o.filled_quote == D("150.5")
    assert o.filled_quote / o.filled_qty == D("150.5") / D("1.5")
    assert o.fee == D("0.003") and o.fee_asset == "BTC"
    bal = b.balances()
    assert bal["USDT"].free == D("849.5") and bal["USDT"].locked == 0   # leftover lock released
    assert bal["BTC"].free == D("1.497") and bal["BTC"].locked == 0
    assert b.open_orders() == []


def test_sell_walks_bids_fee_in_quote(book, sym_rules, clk):
    b = make(book, sym_rules, clk, btc="4")
    o = send(b, "s", Side.SELL, qty="4", price="98")
    # 3 @ 99 + 1 @ 98 = 395 ; taker fee in QUOTE = 395 * 0.002 = 0.79
    assert o.status is OrderStatus.FILLED and o.filled_quote == D("395")
    assert o.fee == D("0.79") and o.fee_asset == "USDT"
    assert b.balances()["USDT"].free == D("1000") + D("395") - D("0.79")
    assert b.balances()["BTC"].free == 0 and b.balances()["BTC"].locked == 0


def test_partial_fill_on_thin_book_remainder_rests(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    o = send(b, "p", qty="5", price="101")
    # only 1 @ 100 + 2 @ 101 inside the limit = 302 for qty 3
    assert o.status is OrderStatus.PARTIALLY_FILLED
    assert o.filled_qty == D("3") and o.filled_quote == D("302") and o.fee == D("0.006")
    bal = b.balances()["USDT"]
    assert bal.free == D("1000") - D("505") and bal.locked == D("505") - D("302")
    assert [x.client_id for x in b.open_orders()] == ["p"]
    # a later snapshot that only touches the limit (ask 101): no maker fill
    book.set([("101", "9")], [("99", "3")])
    b.step()
    assert b.get_order("p").filled_qty == D("3")
    # then 1 visible through the limit (100.99): maker fill AT the limit price 101
    book.set([("100.99", "1"), ("101", "9")], [("99", "3")])
    b.step()
    p = b.get_order("p")
    assert p.status is OrderStatus.PARTIALLY_FILLED and p.filled_qty == D("4")
    assert p.filled_quote == D("302") + D("101") and p.fee == D("0.006") + D("0.001")   # 3 taker + 1 maker


def test_slippage_bps_extra_hits_taker_but_never_beyond_limit(book, sym_rules, clk):
    b = make(book, sym_rules, clk, slippage_bps_extra=100)   # 1%
    m = send(b, "m", type=OrderType.MARKET, qty="1")
    assert m.filled_quote == D("101.00")                     # 100 * 1.01
    book.set([("100", "5")], [("99", "5")])
    lim = send(b, "l", qty="1", price="100")
    assert lim.filled_quote == D("100")                      # capped at the limit
    sell = send(b, "s", Side.SELL, type=OrderType.MARKET, qty="0.5")
    assert sell.filled_quote == D("0.5") * D("99") * D("0.99")


# ----------------------------------------------------------------------------- resting / maker
def test_resting_order_fills_only_on_later_snapshot_through_limit(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    o = send(b, "r", qty="2", price="99")
    assert o.status is OrderStatus.NEW and o.filled_qty == 0
    bal = b.balances()["USDT"]
    assert bal.locked == D("198") and bal.free == D("802")
    b.step()                                               # same snapshot: nothing
    assert b.get_order("r").filled_qty == 0
    book.set([("99", "50")], [("98", "5")])                # touches the limit: not through, no fill
    b.step()
    assert b.get_order("r").filled_qty == 0
    book.set([("98.99", "1.5"), ("99", "50")], [("98", "5")])   # through by one tick: 1.5 visible
    (chg,) = b.step()
    assert chg.status is OrderStatus.PARTIALLY_FILLED
    assert chg.filled_qty == D("1.5") and chg.filled_quote == D("148.5")   # at the LIMIT price
    assert chg.fee == D("0.0015")                                          # maker rate, base fee
    book.set([("98.50", "10")], [("98", "5")])
    (chg,) = b.step()
    assert chg.status is OrderStatus.FILLED and chg.filled_qty == D("2")
    assert chg.filled_quote == D("198") and chg.fee == D("0.002")
    bal = b.balances()
    assert bal["USDT"].locked == 0 and bal["USDT"].free == D("802")
    assert bal["BTC"].free == D("2") - D("0.002")


def test_resting_sell_needs_bid_through_by_one_tick(book, sym_rules, clk):
    b = make(book, sym_rules, clk, btc="1")
    send(b, "rs", Side.SELL, qty="1", price="105")
    assert b.balances()["BTC"].locked == 1
    book.set([("106", "1")], [("105", "9")])               # touches
    b.step()
    assert b.get_order("rs").filled_qty == 0
    book.set([("106", "1")], [("105.01", "0.4"), ("105", "9")])
    b.step()
    o = b.get_order("rs")
    assert o.filled_qty == D("0.4") and o.filled_quote == D("42.0") and o.fee_asset == "USDT"
    assert o.fee == D("42.0") * MAKER


def test_two_resting_orders_cannot_share_the_same_visible_liquidity(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    send(b, "r1", qty="1", price="99")
    send(b, "r2", qty="1", price="99")
    book.set([("98", "1.25")], [("97", "5")])
    b.step()
    assert b.get_order("r1").filled_qty == D("1") and b.get_order("r2").filled_qty == D("0.25")


# ----------------------------------------------------------------------------- latency
def test_latency_defers_matching_to_a_fresh_snapshot(book, sym_rules, clk):
    b = make(book, sym_rules, clk, latency=1.0)
    o = send(b, "lat", qty="1", price="101")
    assert o.status is OrderStatus.NEW and o.filled_qty == 0
    calls = book.calls
    clk.advance(0.5)
    assert b.step() == [] and book.calls == calls          # not eligible: book not even read
    book.set([("100.5", "9")], [("99", "9")])              # price moved during the latency window
    clk.advance(0.5)
    (chg,) = b.step()
    assert chg.status is OrderStatus.FILLED and chg.filled_quote == D("100.5")


def test_cancel_before_eligible_cancels_with_no_fill(book, sym_rules, clk):
    b = make(book, sym_rules, clk, latency=1.0)
    send(b, "c", qty="1", price="101")
    assert b.cancel_order("c").status is OrderStatus.CANCELED
    clk.advance(5)
    assert b.step() == [] and b.get_order("c").filled_qty == 0
    assert b.balances()["USDT"] == b.balances()["USDT"].__class__(D("1000"), D("0"))


# ----------------------------------------------------------------------------- market orders
def test_market_auto_cancels_when_size_exceeds_depth(sym_rules, clk):
    book = FakeBook()
    book.set([("100", "1"), ("101", "1")], [("99", "5")])
    b = make(book, sym_rules, clk)
    o = send(b, "m", type=OrderType.MARKET, qty="3")
    assert o.status is OrderStatus.CANCELED
    assert o.filled_qty == D("2") and o.filled_quote == D("201")
    bal = b.balances()["USDT"]
    assert bal.locked == 0 and bal.free == D("1000") - D("201")


def test_market_does_not_walk_beyond_two_percent_band(sym_rules, clk):
    book = FakeBook()
    book.set([("100", "1"), ("103", "5")], [("99.5", "5")])
    b = make(book, sym_rules, clk)
    o = send(b, "m", type=OrderType.MARKET, qty="3")
    assert o.status is OrderStatus.CANCELED and o.filled_qty == D("1")   # 103 > 100 * 1.02


def test_market_auto_cancels_when_price_moved_more_than_two_percent(sym_rules, clk):
    book = FakeBook()
    book.set([("100", "10")], [("99", "10")])               # decision mid 99.5
    b = make(book, sym_rules, clk, latency=1.0)
    send(b, "m", type=OrderType.MARKET, qty="1")
    book.set([("103", "10")], [("102", "10")])              # mid 102.5: +3.0%
    clk.advance(1)
    (chg,) = b.step()
    assert chg.status is OrderStatus.CANCELED and chg.filled_qty == 0
    assert b.balances()["USDT"].free == D("1000") and b.balances()["USDT"].locked == 0


def test_market_inside_two_percent_after_latency_fills(sym_rules, clk):
    book = FakeBook()
    book.set([("100", "10")], [("99", "10")])
    b = make(book, sym_rules, clk, latency=1.0)
    send(b, "m", type=OrderType.MARKET, qty="1")
    book.set([("101", "10")], [("100", "10")])              # mid 100.5: +1.0%
    clk.advance(1)
    (chg,) = b.step()
    assert chg.status is OrderStatus.FILLED and chg.filled_quote == D("101")


# ----------------------------------------------------------------------------- stops
def test_stop_limit_sell_triggers_on_mid_then_fills_as_taker(book, sym_rules, clk):
    b = make(book, sym_rules, clk, btc="1")
    o = send(b, "st", Side.SELL, OrderType.STOP_LIMIT, qty="1", price="94", stop="95")
    assert o.status is OrderStatus.PENDING_TRIGGER and b.balances()["BTC"].locked == 1
    book.set([("97", "5")], [("96", "5")])                  # mid 96.5 > 95
    assert b.step() == [] and b.get_order("st").status is OrderStatus.PENDING_TRIGGER
    book.set([("95.5", "5")], [("94.5", "5")])              # mid 95.0 <= 95 -> trigger
    (chg,) = b.step()
    assert chg.status is OrderStatus.FILLED and chg.filled_quote == D("94.5")   # 94.5 >= limit 94
    assert chg.fee == D("94.5") * TAKER and chg.fee_asset == "USDT"


def test_stop_limit_stays_resting_when_limit_not_marketable(book, sym_rules, clk):
    b = make(book, sym_rules, clk, btc="1")
    send(b, "st", Side.SELL, OrderType.STOP_LIMIT, qty="1", price="96", stop="95")
    book.set([("95.5", "5")], [("94.5", "5")])              # triggers, but bid 94.5 < limit 96
    b.step()
    assert b.get_order("st").status is OrderStatus.NEW and b.get_order("st").filled_qty == 0


def test_stop_market_buy_triggers_when_mid_rises(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    send(b, "sb", Side.BUY, OrderType.STOP_MARKET, qty="1", stop="105")
    book.set([("104", "5")], [("103", "5")])
    b.step()
    assert b.get_order("sb").status is OrderStatus.PENDING_TRIGGER
    book.set([("107", "5")], [("105", "5")])                # mid 106
    (chg,) = b.step()
    assert chg.status is OrderStatus.FILLED and chg.filled_quote == D("107")


# ----------------------------------------------------------------------------- guards and ledger
def test_duplicate_client_id_creates_nothing(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    send(b, "dup", qty="1", price="50")
    before = b.balances()
    with pytest.raises(DuplicateClientId):
        send(b, "dup", qty="2", price="50")
    assert len(b.orders) == 1 and b.balances() == before and b.get_order("dup").quantity == 1


def test_locked_balance_and_insufficient_funds(book, sym_rules, clk):
    b = make(book, sym_rules, clk, usdt="100")
    send(b, "a", qty="1", price="60")
    assert b.balances()["USDT"].free == D("40") and b.balances()["USDT"].locked == D("60")
    with pytest.raises(OrderRejected, match="insufficient"):
        send(b, "b", qty="1", price="60")
    assert b.get_order("b") is None
    with pytest.raises(OrderRejected, match="insufficient"):
        send(b, "c", Side.SELL, qty="1", price="150")      # no BTC
    b.cancel_order("a")
    assert b.balances()["USDT"].free == D("100") and b.balances()["USDT"].locked == 0


@pytest.mark.parametrize("kw", [dict(qty="0.0000001", price="50"), dict(qty="1", price="50.001"),
                                dict(qty="1", price=None)])
def test_validation_rejects(book, sym_rules, clk, kw):
    b = make(book, sym_rules, clk)
    with pytest.raises(OrderRejected):
        send(b, "x", **kw)
    assert b.orders == {}


def test_book_source_failure_rejects_and_never_moves_orders(book, sym_rules, clk):
    broken = {"on": False}

    def src(symbol):
        if broken["on"]:
            raise OSError("down")
        return book(symbol)
    b = ShadowBroker(src, sym_rules, {"USDT": D("1000")}, MAKER, TAKER, clk, latency_s=0)
    send(b, "r", qty="1", price="50")
    broken["on"] = True
    assert b.step() == [] and b.get_order("r").status is OrderStatus.NEW
    with pytest.raises(OrderRejected):
        send(b, "n", qty="1", price="50")
    assert any(e["event"] == "book_error" for e in b.events)


def test_cancel_race_fill_before_cancel(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    send(b, "race", qty="1", price="99")
    book.set([("98", "5")], [("97", "5")])
    b.step()                                                # fills fully first
    snap = b.get_order("race")
    after = b.cancel_order("race")
    assert snap.status is OrderStatus.FILLED and after == snap
    assert b.balances()["BTC"].free == D("1") - D("0.001") and b.balances()["USDT"].locked == 0
    assert b.cancel_order("unknown") is None


def test_get_order_is_idempotent_and_reads_do_not_mutate(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    send(b, "i", qty="2", price="99")
    first = b.get_order("i")
    calls = book.calls
    for _ in range(3):
        assert b.get_order("i") == first and b.open_orders() == [first]
        b.balances()
    assert book.calls == calls and b.get_order("nope") is None
    assert b.open_orders("ETHUSDT") == [] and b.fee_rates().maker == MAKER


def test_quote_and_fee_rates(book, sym_rules, clk):
    b = make(book, sym_rules, clk)
    q = b.quote(SYM)
    assert (q.bid, q.ask) == (D("99"), D("100"))
    assert b.fee_rates().taker == TAKER


# ----------------------------------------------------------------------------- journal + report
def _scenario(path, sym_rules):
    clk, book = MockClock(), FakeBook()
    book.set([("100", "1"), ("101", "1")], [("98", "5")])           # mid 99
    b = ShadowBroker(book, sym_rules, {"USDT": D("1500"), "BTC": D("0")}, MAKER, TAKER, clk,
                     latency_s=0, journal_path=path)
    send(b, "full", type=OrderType.MARKET, qty="1")                 # 1 @ 100 -> slip 1/99 = 101.01 bps
    send(b, "part", qty="3", price="101")                           # 1 @ 101 then rests (partial)
    send(b, "rest", qty="1", price="90")                            # never fills
    with pytest.raises(OrderRejected):
        send(b, "toobig", qty="100", price="100")                   # insufficient balance (10000)
    with pytest.raises(DuplicateClientId):
        send(b, "full", qty="1", price="100")
    send(b, "mkt", type=OrderType.MARKET, qty="5")                  # book is depleted? no: fresh snap
    b.cancel_order("rest")
    return b


def test_journal_and_report(sym_rules, tmp_path):
    path = tmp_path / "sub" / "shadow.jsonl"
    b = _scenario(path, sym_rules)
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["event"] for x in lines] == [e["event"] for e in b.events]
    fills = [x for x in lines if x["event"] == "fill"]
    assert fills[0]["client_id"] == "full" and fills[0]["price"] == "100" and fills[0]["liquidity"] == "taker"
    rep = shadow_report(path)
    assert rep["orders"] == 4 and rep["rejects"] == 1 and rep["duplicate_client_ids"] == 1
    assert rep["reject_reasons"] == {"insufficient balance": 1}
    assert rep["filled_orders"] == 1 and rep["partial_orders"] == 2 and rep["unfilled_orders"] == 1
    assert rep["canceled_orders"] == 2 and rep["auto_canceled_orders"] == 1
    assert rep["fills"] == 5 and rep["taker_fills"] == 5 and rep["maker_fills"] == 0
    # fills: full 1@100 (+1.0101%), part 1@101, mkt 1@100 and 1@101 (book not depleted across calls)
    assert fills[0]["slippage_bps"].startswith("101.0101")
    assert rep["avg_slippage_bps"] == pytest.approx(
        sum(float(f["slippage_bps"]) * float(f["quote"]) for f in fills)
        / sum(float(f["quote"]) for f in fills))
    assert rep["worst_slippage_bps"] == pytest.approx(max(float(f["slippage_bps"]) for f in fills))
    assert rep["avg_taker_slippage_bps"] == pytest.approx(rep["avg_slippage_bps"])


def test_deterministic_journal(sym_rules, tmp_path):
    _scenario(tmp_path / "a.jsonl", sym_rules)
    _scenario(tmp_path / "b.jsonl", sym_rules)
    assert (tmp_path / "a.jsonl").read_text() == (tmp_path / "b.jsonl").read_text()


def test_report_counts_maker_fill_and_triggers(sym_rules, tmp_path, clk):
    path = tmp_path / "j.jsonl"
    book = FakeBook()
    book.set([("100", "5")], [("99", "5")])
    b = ShadowBroker(book, sym_rules, {"USDT": D("1000"), "BTC": D("1")}, MAKER, TAKER, clk,
                     latency_s=0, journal_path=path)
    send(b, "m", qty="1", price="99")
    send(b, "st", Side.SELL, OrderType.STOP_MARKET, qty="1", stop="90")
    book.set([("88", "5")], [("87", "5")])
    b.step()
    rep = shadow_report(path)
    assert rep["maker_fills"] == 1 and rep["taker_fills"] == 1 and rep["stop_triggers"] == 1
    assert rep["filled_orders"] == 2


# ----------------------------------------------------------------------------- no network
FORBIDDEN = {"urllib", "socket", "http", "ssl", "requests", "httpx", "aiohttp", "asyncio", "threading",
             "subprocess", "ftplib", "smtplib", "wallexbot.exchange.wallex_public",
             "wallexbot.exchange.wallex_private", "wallexbot.exchange.auth"}


def test_shadow_module_has_no_network_capability():
    tree = ast.parse(Path(shadow_mod.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    bad = {m for m in imported if m in FORBIDDEN or m.split(".")[0] in FORBIDDEN}
    assert not bad, bad


# ----------------------------------------------------------------------------- OMS conformance
class Rig:
    """Same wiring as tests/conftest.py Rig, but the exchange is a ShadowBroker (latency 0)."""

    def __init__(self, cfg, sym_rules, clock, store):
        self.cfg, self.clock, self.store = cfg, clock, store
        self.rules = {r.symbol: r for r in sym_rules}
        self.book = FakeBook(clock)
        self.book.set_price("60000.00", "60010.00")
        self.broker = ShadowBroker(self.book, sym_rules, {"USDT": D("10000")}, D("0.0035"), D("0.0035"),
                                   clock, latency_s=0)
        self.ks = KillSwitch(store)
        self.risk = RiskEngine(cfg, self.rules, self.ks)
        self.oms = OMS(store, self.broker, self.risk, self.ks, approve=auto_approve, clock=clock,
                       not_found_grace_s=30)

    def set_price(self, bid, ask, depth="1000"):
        self.book.set_price(bid, ask, depth)

    def submit(self, intent):
        acct = snapshot_account(self.store, self.broker.balances(), "USDT",
                                {SYM: self.broker.quote(SYM)}, self.clock())
        return self.oms.submit(intent, self.broker.quote(intent.symbol), acct)


@pytest.fixture
def rig(cfg, sym_rules, clock, store):
    return Rig(cfg, sym_rules, clock, store)


def buy(qty="0.010000", price="60010.00", type=OrderType.LIMIT, sig="sig1"):
    return OrderIntent(SYM, Side.BUY, type, D(qty), None if price is None else D(price), sig)


def sell(qty, sig="exit1", type=OrderType.MARKET, price=None, stop=None):
    return OrderIntent(SYM, Side.SELL, type, D(qty), None if price is None else D(price), sig,
                       reduce_only=True, stop_price=None if stop is None else D(stop))


def test_oms_happy_path_and_resubmit_never_sends_twice(rig):
    i = buy()
    res = rig.submit(i)
    assert res.sent and res.state is S.FILLED and res.decision.approved
    assert [r["to_state"] for r in rig.store.rows("intent_state_log")] == \
        ["proposed", "risk_approved", "submitted", "filled"]
    pos = rig.oms.positions()[SYM]
    assert pos.quantity == D("0.009965") and pos.meta["cost_basis"] == D("600.10")
    again = rig.submit(i)
    assert again.sent is False and again.state is S.FILLED
    assert rig.broker.submit_calls == 1 and len(rig.broker.orders) == 1
    assert len(rig.store.rows("fills")) == 1


def test_oms_risk_rejection_never_reaches_broker(rig):
    res = rig.submit(buy(qty="0.0100001"))
    assert res.state is S.REJECTED and not res.sent and rig.broker.submit_calls == 0


def test_oms_duplicate_client_id_adopts_existing_order(rig):
    i = buy()
    rig.broker.submit_order(client_id=i.client_id, symbol=SYM, side=Side.BUY, type=OrderType.LIMIT,
                            quantity=i.quantity, price=i.price)
    res = rig.submit(i)
    assert res.state is S.UNKNOWN_PENDING and len(rig.broker.orders) == 1
    assert rig.oms.reconcile() == {i.client_id: S.FILLED}
    assert rig.oms.positions()[SYM].quantity == D("0.009965")


def test_oms_kill_switch_blocks_entry_but_not_exit(rig):
    rig.submit(buy())
    rig.ks.engage("test")
    assert rig.submit(buy(sig="s2")).state is S.REJECTED
    assert rig.submit(sell("0.009965")).state is S.FILLED
    assert rig.oms.positions()[SYM].quantity == 0


def test_oms_round_trip_realized_pnl(rig):
    rig.submit(buy())
    rig.set_price("59000.00", "59010.00")
    rig.submit(sell("0.009965"))
    pos = rig.oms.positions()[SYM]
    expected = D("0.009965") * D("59000.00") * (1 - D("0.0035")) - D("600.10")
    assert pos.quantity == 0 and pos.realized_pnl == expected


def test_oms_stop_loss_fills_on_trigger_via_step_and_reconcile(rig):
    rig.submit(buy())
    i = sell("0.009965", type=OrderType.STOP_MARKET, stop="59500.00", sig="stop1")
    assert rig.submit(i).state is S.SUBMITTED
    rig.set_price("59400.00", "59410.00")
    rig.broker.step()
    assert rig.oms.reconcile() == {i.client_id: S.FILLED}
    assert rig.oms.positions()[SYM].quantity == 0


def test_oms_market_order_partial_then_canceled(rig):
    rig.set_price("60000.00", "60010.00", depth="0.004")
    assert rig.submit(buy(type=OrderType.MARKET, price=None)).state is S.CANCELED
    assert rig.oms.positions()[SYM].quantity == D("0.004") - D("0.004") * D("0.0035")


def test_oms_resting_order_fills_on_later_snapshot_via_reconcile(rig):
    i = buy(price="59000.00")
    assert rig.submit(i).state is S.SUBMITTED
    rig.set_price("58000.00", "58900.00")                  # ask through the limit by far
    rig.broker.step()
    assert rig.oms.reconcile() == {i.client_id: S.FILLED}
    assert rig.oms.positions()[SYM].quantity == D("0.010000") - D("0.010000") * D("0.0035")
    assert rig.oms.reconcile() == {}


def test_oms_cancel_stale_entries_only(rig):
    entry = buy(price="59000.00")
    assert rig.submit(entry).state is S.SUBMITTED
    assert rig.oms.cancel_stale(300) == []
    rig.clock.advance(301)
    assert rig.oms.cancel_stale(300) == [entry.client_id]
    assert rig.store.get_intent(entry.client_id).state is S.CANCELED
    assert rig.broker.balances()["USDT"].locked == 0


def test_oms_cancel_stale_skips_reduce_only(rig):
    rig.submit(buy())
    i = sell("0.009965", type=OrderType.LIMIT, price="70000.00", sig="tp")
    assert rig.submit(i).state is S.SUBMITTED
    rig.clock.advance(10_000)
    assert rig.oms.cancel_stale(300) == []
    assert rig.store.get_intent(i.client_id).state is S.SUBMITTED
