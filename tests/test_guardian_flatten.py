"""Flatten procedure on MockWallex with fault injection.

Pass criterion everywhere: no duplicate / runaway sells, every case ends in a defined state
(flat, dust, resting_exits, or needs_review with an operator alert)."""
from decimal import Decimal

import pytest

from wallexbot.guardian.flatten import FlattenConfig, flatten_all, recover_guardian_intents
from wallexbot.store import Store
from wallexbot.types import IntentState, OrderIntent, OrderType, Side

D = Decimal
SYM, ETH = "BTCUSDT", "ETHUSDT"
S = IntentState


@pytest.fixture
def gstore(rig):
    """The guardian's own connection to the same database."""
    s = Store(rig.store.path, clock=rig.clock)
    yield s
    s.close()


def buy(rig, sym=SYM, qty="0.010000", sig="entry-1"):
    res = rig.submit(OrderIntent(sym, Side.BUY, OrderType.MARKET, D(qty), None, sig))
    assert res.state is S.FILLED, res
    return res


def flatten(rig, gstore, *, hard=True, cause="hard", **cfg):
    # The guardian's own OMS on its own connection; sleeping advances the mock clock.
    from wallexbot.oms import OMS
    oms = OMS(gstore, rig.broker, rig.risk, rig.ks, clock=rig.clock, not_found_grace_s=30)
    from wallexbot.risk.killswitch import KillSwitch
    return flatten_all(gstore, rig.broker, oms, KillSwitch(gstore), cause=cause, hard=hard,
                       config=FlattenConfig(**cfg), clock=rig.clock, sleep=rig.clock.advance)


def sells(rig, sym=SYM):
    return [o for o in rig.broker.orders.values() if o.side is Side.SELL and o.symbol == sym]


def base_total(rig, asset="BTC"):
    b = rig.broker.balances().get(asset)
    return D(0) if b is None else b.total


def alerts(store):
    return [r for r in store.rows("errors") if r["source"] == "guardian.alert"]


# ------------------------------------------------------------------ happy path / sizing
def test_hard_flatten_ends_flat_with_one_market_sell_per_position(rig, gstore):
    rig.broker.set_price(ETH, D("3000.00"), D("3001.00"))
    buy(rig)
    buy(rig, ETH, "0.1000", "entry-eth")
    calls = rig.broker.submit_calls
    res = flatten(rig, gstore)
    assert res.flat and not res.needs_review and res.alerts == []
    assert rig.broker.submit_calls - calls == 2
    for sym in (SYM, ETH):
        (o,) = sells(rig, sym)
        assert o.type is OrderType.MARKET and o.status.value == "FILLED"
    assert res.symbols[SYM].status == "flat" and base_total(rig) == 0
    assert rig.store.latest_positions()[SYM].quantity == 0
    # ETH: 0.1 bought, 0.00035 base fee -> 0.09965; amount precision 4 sells 0.0996, 0.00005 is dust
    assert res.symbols[ETH].status == "dust" and base_total(rig, "ETH") == D("0.00005")
    ks = rig.store.latest_killswitch()
    assert ks.engaged and ks.actor == "guardian"


def test_exit_is_sized_from_confirmed_exchange_balance_not_ledger(rig, gstore):
    buy(rig)
    rig.broker._free["BTC"] -= D("0.005000")       # e.g. a manual sale on the exchange
    res = flatten(rig, gstore)
    (o,) = sells(rig)
    assert o.quantity == D("0.004965")              # what the exchange holds, not the ledger's 0.009965
    assert base_total(rig) == 0
    assert res.needs_review and res.symbols[SYM].reason == "ledger_without_balance"
    assert len(alerts(gstore)) == 1


def test_depth_split_each_child_at_most_half_the_visible_depth(rig, gstore):
    buy(rig)
    rig.broker.set_book(SYM, [(D("60000.00"), D("0.004"))], [(D("60010.00"), D("0.004"))])
    res = flatten(rig, gstore)
    qtys = [o.quantity for o in sells(rig)]
    assert res.flat and base_total(rig) == 0
    assert all(q <= D("0.002") for q in qtys)
    assert qtys == [D("0.002")] * 4 + [D("0.001965")]
    assert sum(qtys) == D("0.009965")


def test_depth_beyond_the_market_band_is_not_counted(rig, gstore):
    buy(rig)
    # Only 0.001 within 2% of the best bid; deep liquidity 5% lower must not size a MARKET child.
    rig.broker.set_book(SYM, [(D("60000.00"), D("0.001")), (D("57000.00"), D("100"))],
                        [(D("60010.00"), D("1"))])
    flatten(rig, gstore, max_orders_per_symbol=3)
    assert all(o.quantity <= D("0.0005") for o in sells(rig))


# ------------------------------------------------------------------ faults
def test_partial_fill_then_cancel_then_remainder(rig, gstore):
    buy(rig)
    rig.broker.queue_fault("partial")
    res = flatten(rig, gstore)
    first, second = sells(rig)
    assert first.status.value == "CANCELED" and first.filled_qty == D("0.004982")
    assert second.quantity == D("0.004983") and second.status.value == "FILLED"
    assert res.flat and base_total(rig) == 0 and res.symbols[SYM].failures == 0


def test_order_timeout_after_accept_never_resubmits(rig, gstore):
    buy(rig)
    calls = rig.broker.submit_calls
    rig.broker.queue_fault("timeout_after_accept")
    res = flatten(rig, gstore)
    assert rig.broker.submit_calls - calls == 1
    assert res.flat and base_total(rig) == 0
    cid = res.symbols[SYM].orders[0]
    path = [r["to_state"] for r in gstore.rows("intent_state_log") if r["client_id"] == cid]
    assert path == ["proposed", "risk_approved", "unknown_pending", "filled"]


def test_timeout_with_lookups_down_waits_then_alerts_and_never_resubmits(rig, gstore):
    buy(rig)
    calls = rig.broker.submit_calls
    rig.broker.queue_fault("timeout_after_accept")
    rig.broker.lookup_failures = 10_000
    res = flatten(rig, gstore)
    assert rig.broker.submit_calls - calls == 1
    assert res.needs_review and not res.flat
    assert res.symbols[SYM].reason == "max_failed_attempts"
    assert {a["message"] for a in alerts(gstore)} >= {
        "flatten needs operator review: BTCUSDT max_failed_attempts"}
    # lookups come back: the next pass reconciles the original order instead of sending another
    rig.broker.lookup_failures = 0
    res2 = flatten(rig, gstore)
    assert rig.broker.submit_calls - calls == 1
    assert res2.flat and base_total(rig) == 0


def test_three_failed_attempts_alert_once_and_stop(rig, gstore):
    buy(rig)
    calls = rig.broker.submit_calls
    rig.broker.fault_mode = "reject"
    res = flatten(rig, gstore, hard=False, cause="operator")
    assert rig.broker.submit_calls - calls == 3
    assert res.needs_review and res.symbols[SYM].status == "needs_review"
    assert res.symbols[SYM].reason == "max_failed_attempts"
    assert len(alerts(gstore)) == 1
    types = [gstore.get_intent(c).intent.type for c in res.symbols[SYM].orders]
    assert types == [OrderType.LIMIT, OrderType.MARKET, OrderType.MARKET]  # escalated after the LIMIT failed
    # a second pass of the same episode does not try again and does not re-alert
    res2 = flatten(rig, gstore, hard=False, cause="operator")
    assert rig.broker.submit_calls - calls == 3
    assert res2.needs_review and len(alerts(gstore)) == 1


def test_limit_first_escalates_to_market_after_timeout(rig, gstore):
    buy(rig)
    rig.broker.queue_fault("rest")       # the LIMIT exit rests and never fills
    t0 = rig.clock()
    res = flatten(rig, gstore, hard=False, cause="operator")
    limit, market = sells(rig)
    assert limit.type is OrderType.LIMIT and limit.status.value == "CANCELED" and limit.filled_qty == 0
    assert limit.price == D("59880.00")  # bid 60000 minus 20 bps, rounded down to price precision
    assert market.type is OrderType.MARKET and market.status.value == "FILLED"
    assert rig.clock() - t0 >= 10        # waited the LIMIT timeout before canceling
    assert res.flat and res.symbols[SYM].failures == 1


def test_flatten_twice_in_a_row_is_idempotent(rig, gstore):
    buy(rig)
    first = flatten(rig, gstore)
    calls, n_intents = rig.broker.submit_calls, len(gstore.rows("order_intents"))
    second = flatten(rig, gstore)
    assert first.flat and second.flat
    assert rig.broker.submit_calls == calls and len(gstore.rows("order_intents")) == n_intents
    assert SYM not in second.symbols          # nothing held any more: nothing to do
    assert len(sells(rig)) == 1


def test_same_durable_state_gives_the_same_client_id(rig, gstore):
    """A crash between OMS.submit and the next poll: the rerun derives the identical intent, which
    the OMS refuses to send again."""
    buy(rig)
    rig.broker.queue_fault("rest")
    first = flatten(rig, gstore, hard=False, cause="operator")
    cid = first.symbols[SYM].orders[0]
    calls = rig.broker.submit_calls
    from wallexbot.oms import OMS
    oms = OMS(gstore, rig.broker, rig.risk, rig.ks, clock=rig.clock)
    again = OrderIntent(SYM, Side.SELL, OrderType.LIMIT, D("0.009965"), D("59880.00"),
                        "gflat:" + first.episode + ":BTCUSDT:0", reduce_only=True)
    assert again.client_id == cid
    res = oms.submit(again, rig.broker.quote(SYM), rig.account())
    assert res.sent is False and rig.broker.submit_calls == calls


def test_unresolved_exit_blocks_reruns_until_confirmed_absent(rig, gstore):
    """A rerun whose exit is still ambiguous waits on it; it never mints a second order."""
    buy(rig)
    calls = rig.broker.submit_calls
    rig.broker.queue_fault("timeout_before_accept")   # nothing created; OMS: unknown_pending
    rig.broker.lookup_failures = 10_000               # ...and it cannot be resolved yet
    flatten(rig, gstore)
    flatten(rig, gstore)
    assert rig.broker.submit_calls - calls == 1
    rig.broker.lookup_failures = 0                    # resolves to not-found after the OMS grace
    res = flatten(rig, gstore)
    assert res.flat and base_total(rig) == 0
    assert rig.broker.submit_calls - calls == 2       # exactly one replacement, after confirmation


def test_kill_switch_never_blocks_guardian_exits(rig, gstore):
    buy(rig)
    rig.ks.engage("operator stop", actor="operator")
    res = flatten(rig, gstore)
    assert res.flat and base_total(rig) == 0
    assert rig.store.latest_killswitch().actor == "operator"   # already engaged: no extra row
    blocked = rig.submit(OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.001000"), None, "entry-2"))
    assert blocked.state is S.REJECTED


def test_cancels_entry_orders_and_protective_stop_before_selling(rig, gstore):
    buy(rig)
    entry = rig.submit(OrderIntent(SYM, Side.BUY, OrderType.LIMIT, D("0.001000"), D("59000.00"), "entry-2"))
    stop = rig.submit(OrderIntent(SYM, Side.SELL, OrderType.STOP_MARKET, D("0.009965"), None, "stop-1",
                                  reduce_only=True, stop_price=D("55000.00")))
    assert entry.state is S.SUBMITTED and stop.state is S.SUBMITTED
    assert rig.broker.balances()["BTC"].free == 0          # the stop locks the whole position
    res = flatten(rig, gstore)
    assert res.canceled_entries == [entry.client_id]
    assert rig.broker.orders[entry.client_id].status.value == "CANCELED"
    assert rig.broker.orders[stop.client_id].status.value == "CANCELED"
    assert gstore.get_intent(stop.client_id).state is S.CANCELED
    assert res.flat and base_total(rig) == 0
    kinds = [e["kind"] for e in gstore.audit_events("guardian")]
    assert kinds.index("cancel_entry") < kinds.index("cancel_protective_stop") < kinds.index("flatten_exit")


def test_protective_stop_kept_when_flag_off(rig, gstore):
    buy(rig)
    stop = rig.submit(OrderIntent(SYM, Side.SELL, OrderType.STOP_MARKET, D("0.005000"), None, "stop-1",
                                  reduce_only=True, stop_price=D("55000.00")))
    res = flatten(rig, gstore, cancel_protective_stops=False)
    assert rig.broker.orders[stop.client_id].status.value == "PENDING_TRIGGER"
    (o,) = [o for o in sells(rig) if o.client_id != stop.client_id]
    assert o.quantity == D("0.004965")                      # only the unlocked part
    assert res.symbols[SYM].status == "resting_exits" and not res.flat and not res.needs_review


def test_dry_run_plans_but_never_writes_to_the_broker(rig, gstore):
    buy(rig)
    rig.submit(OrderIntent(SYM, Side.BUY, OrderType.LIMIT, D("0.001000"), D("59000.00"), "entry-2"))
    calls = rig.broker.submit_calls
    res = flatten(rig, gstore, dry_run=True)
    assert rig.broker.submit_calls == calls
    assert all(o.status.value != "CANCELED" for o in rig.broker.orders.values())
    assert res.symbols[SYM].status == "dry_run" and not res.flat
    assert {"plan_cancel_entries", "plan_exit"} <= {e["kind"] for e in gstore.audit_events("guardian")}


def test_unknown_depth_falls_back_to_a_bounded_limit(rig, gstore):
    buy(rig)
    from wallexbot.oms import OMS
    from wallexbot.risk.killswitch import KillSwitch

    def no_depth(sym):
        raise TimeoutError("depth endpoint down")

    oms = OMS(gstore, rig.broker, rig.risk, rig.ks, clock=rig.clock)
    res = flatten_all(gstore, rig.broker, oms, KillSwitch(gstore), cause="hard", hard=True,
                      clock=rig.clock, sleep=rig.clock.advance, depth_fn=no_depth)
    (o,) = sells(rig)
    assert o.type is OrderType.LIMIT and o.price == D("59880.00")
    assert res.flat


def test_recover_guardian_intents_touches_only_the_guardian(rig, gstore):
    mine = OrderIntent(SYM, Side.SELL, OrderType.MARKET, D("0.001000"), None, "gflat:2025-01-01-hard:BTCUSDT:0",
                       reduce_only=True)
    other = OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.001000"), None, "strategy-sig")
    for i in (mine, other):
        gstore.append_intent(i)
        gstore.set_intent_state(i.client_id, S.RISK_APPROVED)
    assert recover_guardian_intents(gstore, rig.clock()) == [mine.client_id]
    assert gstore.get_intent(mine.client_id).state is S.UNKNOWN_PENDING
    assert gstore.get_intent(other.client_id).state is S.RISK_APPROVED


def test_guardian_audit_rows_have_actor_guardian(rig, gstore):
    buy(rig)
    flatten(rig, gstore)
    rows = gstore.rows("audit_events")
    assert rows and {r["actor"] for r in rows} == {"guardian"}
    assert {"flatten_start", "engage_kill_switch", "flatten_exit", "flatten_done"} <= {r["kind"] for r in rows}
    assert [r["actor"] for r in gstore.rows("killswitch_events")] == ["guardian"]
