"""Guardian.tick() on MockWallex: drills for a dead strategy loop, breaches, stale feed, mismatch."""
from decimal import Decimal

import pytest

from wallexbot.guardian.flatten import FlattenConfig
from wallexbot.guardian.main import build_guardian
from wallexbot.guardian.monitor import GuardianConfig
from wallexbot.risk.engine import utc_day
from wallexbot.store import Store
from wallexbot.types import IntentState, OrderIntent, OrderType, Side

D = Decimal
SYM = "BTCUSDT"
S = IntentState
BID, ASK = D("60000.00"), D("60010.00")


@pytest.fixture
def gstore(rig):
    s = Store(rig.store.path, clock=rig.clock)   # the guardian's own connection
    yield s
    s.close()


@pytest.fixture
def make(rig, gstore):
    def _make(**kw):
        return build_guardian(gstore, rig.broker, rig.rules, rig.cfg, GuardianConfig(**kw), clock=rig.clock,
                              sleep=rig.clock.advance)
    return _make


def buy(rig, qty="0.010000", sig="entry-1"):
    res = rig.submit(OrderIntent(SYM, Side.BUY, OrderType.MARKET, D(qty), None, sig))
    assert res.state is S.FILLED
    return res


def live(rig, seconds):
    """Time passes; the strategy loop and the feed are alive."""
    rig.clock.advance(seconds)
    rig.broker.set_price(SYM, BID, ASK)
    rig.broker.set_price("ETHUSDT", D("3000.00"), D("3001.00"))
    rig.store.heartbeat("strategy")


def entry_blocked(rig):
    res = rig.submit(OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.001000"), None, f"probe-{rig.clock()}"))
    return res.state is S.REJECTED and "kill_switch_engaged" in res.decision.reasons


def sells(rig):
    return [o for o in rig.broker.orders.values() if o.side is Side.SELL]


# ------------------------------------------------------------------ healthy
def test_healthy_tick_records_sod_hwm_from_exchange_and_does_nothing(rig, gstore, make):
    buy(rig)
    live(rig, 1)
    g = make()
    rep = g.tick()
    expected = rig.broker.balances()["USDT"].total + D("0.009965") * BID
    assert rep.equity == expected
    assert rep.conditions == [] and rep.actions == []
    assert gstore.sod_equity(utc_day(rig.clock())) is not None and gstore.equity_hwm() >= expected
    assert not rig.ks.is_engaged()
    live(rig, 5)
    assert g.tick().conditions == []                     # exact fee accounting: no mismatch either


# ------------------------------------------------------------------ strategy-loop-killed drill
def test_strategy_loop_killed_drill(rig, make):
    buy(rig)
    live(rig, 1)
    g = make(heartbeat_timeout_s=60)
    assert g.tick().actions == []
    for _ in range(3):            # the strategy loop dies: the feed stays fresh, heartbeats stop
        rig.clock.advance(25)
        rig.broker.set_price(SYM, BID, ASK)
        rig.broker.set_price("ETHUSDT", D("3000.00"), D("3001.00"))
        rep = g.tick()
    assert "heartbeat_lost" in rep.conditions and "engage_kill_switch" in rep.actions
    ks = rig.store.latest_killswitch()
    assert ks.engaged and ks.actor == "guardian" and "heartbeat_lost" in ks.reason
    assert entry_blocked(rig)
    assert sells(rig) == []                               # a dead loop alone does not flatten


def test_heartbeat_never_written_counts_from_guardian_start(rig, make):
    g = make(heartbeat_timeout_s=60)
    rig.broker.set_price("ETHUSDT", D("3000.00"), D("3001.00"))
    assert "heartbeat_lost" not in g.tick().conditions
    rig.clock.advance(61)
    rig.broker.set_price(SYM, BID, ASK)
    rig.broker.set_price("ETHUSDT", D("3000.00"), D("3001.00"))
    assert "heartbeat_lost" in g.tick().conditions


# ------------------------------------------------------------------ breaches
def test_soft_breach_engages_kill_switch_without_selling(rig, gstore, make):
    buy(rig)
    live(rig, 1)
    gstore.append_equity_hwm(D("10900"))     # equity ~9998 -> drawdown ~8.3%: soft (8%), not hard (12%)
    rep = make().tick()
    assert "drawdown_soft" in rep.conditions and rep.flatten is None
    assert rig.store.latest_killswitch().actor == "guardian"
    assert entry_blocked(rig) and sells(rig) == []


def test_hard_drawdown_breach_flattens_to_flat(rig, gstore, make):
    buy(rig)
    live(rig, 1)
    gstore.append_equity_hwm(D("20000"))     # drawdown ~50%: hard
    rep = make().tick()
    assert "drawdown_hard" in rep.conditions and "flatten:hard" in rep.actions
    assert rep.flatten.flat and not rep.flatten.needs_review
    (o,) = sells(rig)
    assert o.type is OrderType.MARKET and o.quantity == D("0.009965")
    assert rig.broker.balances()["BTC"].total == 0 and rig.store.latest_positions()[SYM].quantity == 0
    assert entry_blocked(rig)
    # breach persists (equity stays below the HWM) but nothing is held: no further orders
    live(rig, 5)
    rep2 = make().tick()
    assert "drawdown_hard" in rep2.conditions and rep2.flatten is None and len(sells(rig)) == 1


def test_hard_daily_loss_breach_flattens(rig, gstore, make):
    buy(rig)
    rig.clock.advance(86400)                 # a new UTC day whose SOD was recorded higher
    rig.broker.set_price(SYM, BID, ASK)
    rig.broker.set_price("ETHUSDT", D("3000.00"), D("3001.00"))
    rig.store.heartbeat("strategy")
    gstore.append_equity_sod(utc_day(rig.clock()), D("11000"))
    rep = make().tick()
    assert "daily_loss_hard" in rep.conditions and rep.flatten.flat


# ------------------------------------------------------------------ stale feed
def test_stale_feed_freezes_entries_then_holds_by_default(rig, make):
    buy(rig)
    live(rig, 1)
    g = make()
    assert g.tick().conditions == []
    rig.clock.advance(31)                    # > max_data_age_s (30): no fresh quote
    rig.store.heartbeat("strategy")
    rep = g.tick()
    assert rep.conditions == ["stale_feed"] and "engage_kill_switch" in rep.actions
    assert "stale_feed" in rig.store.latest_killswitch().reason
    rig.clock.advance(100)                   # > 120 s stale with a position: hold (default)
    rig.store.heartbeat("strategy")
    rep = g.tick()
    assert "stale_hold" in rep.conditions and rep.flatten is None and sells(rig) == []


def test_stale_feed_flatten_policy_sells_limit_first(rig, make):
    buy(rig)
    live(rig, 1)
    g = make(stale_action="flatten")
    g.tick()
    rig.clock.advance(121)
    rig.store.heartbeat("strategy")
    rep = g.tick()
    assert "flatten:stale" in rep.actions and rep.flatten.flat
    (o,) = sells(rig)
    assert o.type is OrderType.LIMIT and o.price == D("59880.00")


def test_stale_feed_does_not_move_sod_or_hwm(rig, gstore, make):
    g = make()
    rig.clock.advance(31)
    rig.broker._free["USDT"] += D("5000")     # a new high, but the feed is stale
    g.tick()
    assert gstore.equity_hwm() is None


def test_invalid_stale_action_refused():
    with pytest.raises(ValueError):
        GuardianConfig(stale_action="panic")
    with pytest.raises(ValueError):
        FlattenConfig(unknown_timeout_s=0)


# ------------------------------------------------------------------ balance mismatch
def test_balance_mismatch_blocks_entries_and_needs_review(rig, gstore, make):
    buy(rig)
    live(rig, 1)
    g = make()
    g.tick()
    rig.broker._free["BTC"] -= D("0.004000")      # the exchange no longer matches the ledger
    live(rig, 5)
    first = g.tick()
    assert first.actions == []                        # one observation could be a race
    live(rig, 5)
    rep = g.tick()
    assert "balance_mismatch:BTC" in rep.conditions
    assert "needs_review" in rep.actions and "engage_kill_switch" in rep.actions
    assert entry_blocked(rig)
    (review,) = gstore.audit_events("guardian", "needs_review")
    assert D(review["payload"]["detail"]["BTC"]["exchange"]) == D("0.005965")
    assert [r["source"] for r in gstore.rows("errors")].count("guardian.needs_review") == 1
    assert rig.store.latest_positions()[SYM].quantity == D("0.009965")   # ledger not "fixed"
    live(rig, 5)
    g.tick()                                          # still mismatched: no duplicate review row
    assert len(gstore.audit_events("guardian", "needs_review")) == 1


def test_mismatch_within_tolerance_is_ignored(rig, make):
    buy(rig)
    live(rig, 1)
    g = make()
    rig.broker._free["BTC"] += D("0.000001")          # one amount step: within tolerance
    for _ in range(3):
        live(rig, 5)
        assert g.tick().conditions == []


def test_in_flight_order_suppresses_mismatch(rig, gstore, make):
    buy(rig)
    live(rig, 1)
    g = make()
    rig.broker._free["BTC"] -= D("0.004000")
    pending = OrderIntent(SYM, Side.SELL, OrderType.MARKET, D("0.001000"), None, "mid-submit", reduce_only=True)
    gstore.append_intent(pending)
    gstore.set_intent_state(pending.client_id, S.RISK_APPROVED)
    for _ in range(3):
        live(rig, 5)
        assert not any(c.startswith("balance_mismatch") for c in g.tick().conditions)
    live(rig, 30)                    # the intent is now stuck, not in flight: no longer masks it
    live(rig, 5)
    g.tick()
    assert "balance_mismatch:BTC" in g.tick().conditions


def test_open_orders_outage_does_not_block_hard_breach_flatten(rig, gstore, make, monkeypatch):
    buy(rig)
    live(rig, 1)
    gstore.append_equity_hwm(D("20000"))
    calls = {"n": 0}
    real = rig.broker.open_orders

    def first_call_fails(symbol=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("open orders endpoint down")
        return real(symbol)

    monkeypatch.setattr(rig.broker, "open_orders", first_call_fails)
    rep = make().tick()
    assert "open_orders_unavailable" in rep.conditions and rep.flatten.flat


# ------------------------------------------------------------------ audit
def test_all_guardian_writes_carry_actor_guardian(rig, gstore, make):
    buy(rig)
    live(rig, 1)
    gstore.append_equity_hwm(D("20000"))
    make().tick()
    guardian_rows = [r for r in gstore.rows("audit_events") if r["actor"] != "strategy"]
    assert guardian_rows and {r["actor"] for r in guardian_rows} == {"guardian"}
    assert {"tick", "engage_kill_switch", "flatten_exit"} <= {r["kind"] for r in guardian_rows}
    assert {r["actor"] for r in gstore.rows("killswitch_events")} == {"guardian"}


def test_quiet_ticks_do_not_spam_the_audit_log(rig, gstore, make):
    live(rig, 1)
    g = make()
    for _ in range(5):
        live(rig, 5)
        g.tick()
    assert len(gstore.audit_events("guardian", "tick")) == 1
