from dataclasses import replace
from decimal import Decimal

import pytest

from wallexbot.exchange.base import OrderStatus
from wallexbot.oms import OMS, LedgerError, apply_fill
from wallexbot.risk.engine import RiskDecision
from wallexbot.store import Store
from wallexbot.types import Fill, IntentState, OrderIntent, OrderType, Position, Side

D = Decimal
S = IntentState
SYM = "BTCUSDT"


def buy(qty="0.010000", price="60010.00", type=OrderType.LIMIT, sig="sig1"):
    return OrderIntent(SYM, Side.BUY, type, D(qty), None if price is None else D(price), sig)


def sell(qty, sig="exit1", type=OrderType.MARKET, price=None, stop=None):
    return OrderIntent(SYM, Side.SELL, type, D(qty), None if price is None else D(price), sig,
                       reduce_only=True, stop_price=None if stop is None else D(stop))


def states(rig):
    return [r["to_state"] for r in rig.store.rows("intent_state_log")]


def test_happy_path_fill_updates_position(rig):
    res = rig.submit(buy())
    assert res.sent and res.state is S.FILLED and res.decision.approved
    assert states(rig) == ["proposed", "risk_approved", "submitted", "filled"]
    pos = rig.oms.positions()[SYM]
    assert pos.quantity == D("0.009965")                       # fee paid in BTC
    assert pos.meta["cost_basis"] == D("600.10")               # basis includes the BTC fee via qty
    assert len(rig.store.rows("fills")) == 1


def test_resubmitting_same_intent_never_sends_twice(rig):
    i = buy()
    assert rig.submit(i).sent is True
    again = rig.submit(i)
    assert again.sent is False and again.state is S.FILLED
    assert rig.broker.submit_calls == 1 and len(rig.broker.orders) == 1


def test_risk_rejection_never_reaches_broker(rig):
    res = rig.submit(buy(qty="0.0100001"))
    assert res.state is S.REJECTED and not res.sent
    assert "quantity_precision" in res.decision.reasons
    assert rig.broker.submit_calls == 0
    assert rig.store.rows("risk_decisions")[0]["approved"] == 0


def test_policy_gate_can_only_narrow(rig):
    rig.oms.approve = lambda d: False
    assert rig.submit(buy()).state is S.REJECTED and rig.broker.submit_calls == 0
    rig.oms.approve = lambda d: 1 / 0
    assert rig.submit(buy(sig="s2")).state is S.REJECTED and rig.broker.submit_calls == 0


def test_risk_engine_exception_rejects(rig):
    class Boom:
        rules = rig.rules

        def check(self, *a):
            raise RuntimeError("x")
    rig.oms.risk = Boom()
    res = rig.submit(buy())
    assert res.state is S.REJECTED and rig.broker.submit_calls == 0


def test_timeout_after_accept_no_duplicate_and_reconcile_resolves(rig):
    rig.broker.queue_fault("timeout_after_accept")
    i = buy()
    res = rig.submit(i)
    assert res.state is S.UNKNOWN_PENDING and res.sent
    assert rig.oms.positions() == {}                            # nothing booked while ambiguous
    # retries of the same intent never POST again
    for _ in range(3):
        assert rig.submit(i).sent is False
    assert rig.broker.submit_calls == 1 and len(rig.broker.orders) == 1
    # an UNKNOWN_PENDING order blocks a second entry on the same symbol
    assert "open_order_exists" in rig.submit(buy(sig="other")).decision.reasons
    assert rig.oms.reconcile() == {i.client_id: S.FILLED}
    assert rig.oms.reconcile() == {}
    assert len(rig.store.rows("fills")) == 1
    assert rig.oms.positions()[SYM].quantity == D("0.009965")


def test_timeout_before_accept_resolves_to_rejected_after_grace(rig):
    rig.broker.queue_fault("timeout_before_accept")
    i = buy()
    assert rig.submit(i).state is S.UNKNOWN_PENDING
    assert rig.oms.reconcile() == {i.client_id: S.UNKNOWN_PENDING}   # within grace: wait
    rig.clock.advance(31)
    assert rig.oms.reconcile() == {i.client_id: S.REJECTED}
    assert rig.broker.submit_calls == 1 and rig.broker.orders == {}
    assert rig.submit(i).sent is False                               # still never resent


def test_reconcile_lookup_failure_keeps_state(rig):
    rig.broker.queue_fault("timeout_before_accept")
    i = buy()
    rig.submit(i)
    rig.clock.advance(60)
    rig.broker.lookup_failures = 1  # open_orders fails -> absence not confirmed
    assert rig.oms.reconcile() == {i.client_id: S.UNKNOWN_PENDING}
    rig.broker.lookup_failures = 2  # open_orders and get_order fail
    assert rig.oms.reconcile() == {i.client_id: S.UNKNOWN_PENDING}
    assert len(rig.store.rows("errors")) >= 3
    assert rig.oms.reconcile() == {i.client_id: S.REJECTED}


def test_partial_fill_tracked_as_deltas(rig):
    rig.broker.queue_fault("partial")
    i = buy()
    assert rig.submit(i).state is S.PARTIALLY_FILLED
    assert rig.oms.positions()[SYM].quantity == D("0.005000") - D("0.005000") * D("0.0035")
    assert rig.oms.reconcile() == {i.client_id: S.PARTIALLY_FILLED}   # no new fills: no new rows
    assert len(rig.store.rows("fills")) == 1
    rig.broker.set_price(SYM, D("60000.00"), D("60010.00"))           # rest fills
    assert rig.oms.reconcile() == {i.client_id: S.FILLED}
    fills = rig.store.rows("fills")
    assert [f["seq"] for f in fills] == [1, 2]
    assert sum(D(f["quantity"]) for f in fills) == D("0.010000")
    assert rig.oms.positions()[SYM].quantity == D("0.009965")


def test_broker_reject_and_429(rig):
    rig.broker.queue_fault("reject", "429")
    a, b = rig.submit(buy(sig="a")), rig.submit(buy(sig="b"))
    assert a.state is S.REJECTED and b.state is S.REJECTED
    assert rig.oms.positions() == {} and rig.broker.orders == {}


def test_duplicate_client_id_adopts_existing_order(rig):
    i = buy()
    # an order with this client_id already exists at the exchange (e.g. local DB lost)
    rig.broker.submit_order(client_id=i.client_id, symbol=SYM, side=Side.BUY, type=OrderType.LIMIT,
                            quantity=i.quantity, price=i.price)
    res = rig.submit(i)
    assert res.state is S.UNKNOWN_PENDING
    assert len(rig.broker.orders) == 1
    assert rig.oms.reconcile() == {i.client_id: S.FILLED}
    assert rig.oms.positions()[SYM].quantity == D("0.009965")


def test_kill_switch_blocks_entry_but_not_exit(rig):
    rig.submit(buy())
    rig.ks.engage("test")
    assert rig.submit(buy(sig="s2")).state is S.REJECTED
    res = rig.submit(sell("0.009965"))
    assert res.state is S.FILLED
    assert rig.oms.positions()[SYM].quantity == 0


def test_last_moment_kill_switch_gate(rig):
    class FlipKS:
        def __init__(self, ks):
            self.ks = ks

        def allows(self, intent):
            return False
    rig.oms.killswitch = FlipKS(rig.ks)  # risk saw it disengaged; OMS gate sees it engaged
    assert rig.submit(buy()).state is S.REJECTED and rig.broker.submit_calls == 0


def test_round_trip_realized_pnl_and_loss_streak(rig):
    rig.submit(buy())
    rig.broker.set_price(SYM, D("59000.00"), D("59010.00"))
    rig.submit(sell("0.009965"))
    pos = rig.oms.positions()[SYM]
    proceeds = D("0.009965") * D("59000.00")
    expected = proceeds * (1 - D("0.0035")) - D("600.10")
    assert pos.quantity == 0 and pos.realized_pnl == expected
    assert rig.store.consecutive_losses()[0] == 1
    snap = rig.store.rows("positions_snapshots")[-1]
    assert D(snap["closed_pnl"]) == pos.realized_pnl


def test_stop_loss_order_fills_on_trigger_via_reconcile(rig):
    rig.submit(buy())
    i = sell("0.009965", type=OrderType.STOP_MARKET, stop="59500.00", sig="stop1")
    assert rig.submit(i).state is S.SUBMITTED
    rig.broker.set_price(SYM, D("59400.00"), D("59410.00"))
    assert rig.oms.reconcile() == {i.client_id: S.FILLED}
    assert rig.oms.positions()[SYM].quantity == 0


def test_market_order_partial_then_canceled(rig):
    rig.broker.set_price(SYM, D("60000.00"), D("60010.00"), depth=D("0.004"))
    res = rig.submit(buy(type=OrderType.MARKET, price=None))
    assert res.state is S.CANCELED
    assert rig.oms.positions()[SYM].quantity == D("0.004") - D("0.004") * D("0.0035")


def test_cancel_stale_entries_only(rig):
    entry = buy(price="59000.00")
    assert rig.submit(entry).state is S.SUBMITTED
    rig.broker.set_price(SYM, D("60100.00"), D("60110.00"))  # own some BTC via another path
    assert rig.oms.cancel_stale(300) == []                   # too young
    rig.clock.advance(301)
    assert rig.oms.cancel_stale(300) == [entry.client_id]
    assert rig.store.get_intent(entry.client_id).state is S.CANCELED
    assert rig.broker.balances()["USDT"].locked == 0


def test_cancel_stale_skips_reduce_only(rig):
    rig.submit(buy())
    i = sell("0.009965", type=OrderType.LIMIT, price="70000.00", sig="tp")
    assert rig.submit(i).state is S.SUBMITTED
    rig.clock.advance(10_000)
    rig.broker.set_price(SYM, D("60000.00"), D("60010.00"))
    assert rig.oms.cancel_stale(300) == []
    assert rig.store.get_intent(i.client_id).state is S.SUBMITTED


# ----------------------------------------------------------------------------- restart recovery
def _restart(rig):
    """Simulate a process restart: new Store + OMS on the same DB file, same exchange."""
    path = rig.store.path
    rig.store.close()
    rig.store = Store(path, clock=rig.clock)
    from wallexbot.risk.killswitch import KillSwitch
    rig.ks = KillSwitch(rig.store)
    rig.risk.killswitch = rig.ks
    rig.oms = OMS(rig.store, rig.broker, rig.risk, rig.ks, clock=rig.clock, not_found_grace_s=30)
    return rig


def test_recover_resolves_ambiguous_after_restart(rig):
    rig.broker.queue_fault("timeout_after_accept")
    i = buy()
    rig.submit(i)
    _restart(rig)
    calls = rig.broker.submit_calls
    assert rig.oms.recover() == {i.client_id: S.FILLED}
    assert rig.broker.submit_calls == calls
    assert rig.oms.positions()[SYM].quantity == D("0.009965")


def test_recover_crash_mid_submit_never_resends(rig):
    # crash after RISK_APPROVED was persisted: one where the POST landed, one where it did not
    landed, lost, proposed = buy(sig="landed"), buy(sig="lost", price="59000.00"), buy(sig="p")
    for i in (landed, lost):
        rig.store.append_intent(i)
        rig.store.set_intent_state(i.client_id, S.RISK_APPROVED)
    rig.store.append_intent(proposed)
    rig.broker.submit_order(client_id=landed.client_id, symbol=SYM, side=Side.BUY, type=OrderType.LIMIT,
                            quantity=landed.quantity, price=landed.price)
    _restart(rig)
    calls = rig.broker.submit_calls
    out = rig.oms.recover()
    assert out[landed.client_id] is S.FILLED
    assert out[lost.client_id] is S.UNKNOWN_PENDING
    assert rig.store.get_intent(proposed.client_id).state is S.REJECTED
    rig.clock.advance(31)
    assert rig.oms.recover() == {lost.client_id: S.REJECTED}
    assert rig.broker.submit_calls == calls
    # submitting a RISK_APPROVED intent again also never resends
    again = buy(sig="again")
    rig.store.append_intent(again)
    rig.store.set_intent_state(again.client_id, S.RISK_APPROVED)
    assert rig.submit(again).state is S.UNKNOWN_PENDING and rig.broker.submit_calls == calls


def test_recover_resting_partial_completes(rig):
    rig.broker.queue_fault("partial")
    i = buy()
    rig.submit(i)
    _restart(rig)
    rig.broker.set_price(SYM, D("60000.00"), D("60010.00"))
    assert rig.oms.recover() == {i.client_id: S.FILLED}
    assert len(rig.store.rows("fills")) == 2


# ----------------------------------------------------------------------------- inconsistency
class _LyingBroker:
    """Wraps MockWallex and corrupts what get_order reports."""

    def __init__(self, inner, mutate):
        self.inner, self.mutate = inner, mutate

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get_order(self, cid):
        o = self.inner.get_order(cid)
        return None if o is None else self.mutate(o)


@pytest.mark.parametrize("mutate", [
    lambda o: replace(o, quantity=o.quantity * 2),                     # not our order
    lambda o: replace(o, filled_qty=D("0.001"), filled_quote=D("60")),  # totals went backwards
    lambda o: replace(o, status=OrderStatus.FILLED, filled_qty=o.quantity + 1),
])
def test_inconsistent_broker_data_needs_review(rig, mutate):
    rig.broker.queue_fault("partial")
    i = buy()
    assert rig.submit(i).state is S.PARTIALLY_FILLED
    fills_before = len(rig.store.rows("fills"))
    rig.oms.broker = _LyingBroker(rig.broker, mutate)
    assert rig.oms.reconcile() == {i.client_id: S.NEEDS_REVIEW}
    assert len(rig.store.rows("fills")) == fills_before        # nothing half-applied
    assert any(e["source"] == "oms.apply" for e in rig.store.rows("errors"))
    assert rig.oms.reconcile() == {}                            # terminal: left for a human


def test_acknowledged_order_missing_needs_review(rig):
    rig.broker.queue_fault("rest")
    i = buy(price="59000.00")
    assert rig.submit(i).state is S.SUBMITTED
    del rig.broker.orders[i.client_id]
    assert rig.oms.reconcile() == {i.client_id: S.NEEDS_REVIEW}


# ----------------------------------------------------------------------------- ledger maths
def test_apply_fill_fee_handling():
    p = Position(SYM)
    f = Fill("c", SYM, Side.BUY, D("100"), D("2"), D("0.01"), "BTC", 0.0)
    p, closed = apply_fill(p, f, D("200"), "BTC", "USDT")
    assert p.quantity == D("1.99") and p.meta["cost_basis"] == D("200") and closed is None
    f2 = Fill("c2", SYM, Side.SELL, D("110"), D("1.99"), D("0.5"), "USDT", 0.0)
    p, closed = apply_fill(p, f2, D("218.9"), "BTC", "USDT")
    assert p.quantity == 0 and closed == p.realized_pnl
    assert closed == D("218.9") - D("0.5") - D("200")  # exact: full close releases the whole basis
    with pytest.raises(LedgerError):
        apply_fill(Position(SYM), Fill("c3", SYM, Side.SELL, D("1"), D("1"), D("0"), "USDT", 0.0),
                   D("1"), "BTC", "USDT")
    with pytest.raises(LedgerError):
        apply_fill(Position(SYM), Fill("c4", SYM, Side.BUY, D("1"), D("1"), D("1"), "WXT", 0.0),
                   D("1"), "BTC", "USDT")


def test_decision_is_persisted(rig):
    rig.submit(buy())
    row = rig.store.rows("risk_decisions")[0]
    assert row["approved"] == 1 and row["client_id"] == buy().client_id
    assert isinstance(RiskDecision(True), RiskDecision)
