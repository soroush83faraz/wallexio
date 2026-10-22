"""Bot exit vs guardian flatten: never two live SELLs for one position.

The bot's exit and the guardian's flatten are two actors on one database (separate Store
connections). Each test suspends one actor at an interleaving point of OMS.submit and runs the
other actor there, then lets both finish; exactly one effective SELL may reach the broker.

Layers under test (oms.py, guardian/flatten.py):
(a) the risk view, the risk decision and PROPOSED -> RISK_APPROVED|REJECTED are one transaction:
    no other connection can see (or act inside) a decided-but-PROPOSED intent;
(b) a SELL still PROPOSED and younger than PROPOSED_COMMIT_WINDOW_S is committed (risk) and
    ambiguous (flatten); an older one is a crash leftover and ignored.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from decimal import Decimal as D

import pytest
from test_bot_helpers import ManualFeed, cheap_config

from wallexbot.bot.loop import Bot
from wallexbot.bot.main import build_stack
from wallexbot.data.candles import Candle
from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.guardian.flatten import FlattenConfig, flatten_all
from wallexbot.guardian.main import build_guardian
from wallexbot.guardian.monitor import GuardianConfig
from wallexbot.oms import PROPOSED_COMMIT_WINDOW_S
from wallexbot.risk.engine import AccountState
from wallexbot.store import Store
from wallexbot.strategy.contract import ExitDecision, Signal
from wallexbot.strategy.sizing import size_position
from wallexbot.types import IntentState, OrderIntent, OrderType, Side, SymbolRules

S = IntentState
B = 900
T = 1_700_000_100 // B * B
SYM = "S00USDT"


class _Engine:
    def evaluate(self, sym, bars, mc):
        last = bars[-1]
        return Signal(sym, last.ts, last.close, last.close, D("1"), last.close - D(2), D("0.9"))


class _Hold:
    def on_bar(self, st, bars):
        return ExitDecision("hold")

    def targets(self, st):
        return ()


def _bars(last_open):
    p = D(100)
    return [Candle(last_open - i * B, p, p + 1, p - 1, p, D(1000)) for i in range(60, -1, -1)]


class Rig:
    """Bot (own Store) + guardian (own Store) on one DB and one MockWallex; the bot holds a position
    and the price is below its stop, so the bot wants to exit and the guardian (hard daily-loss
    breach) wants to flatten."""

    def __init__(self, tmp_path, guardian_sleep_advances=True):
        cfg = cheap_config()
        self.cfg = dataclasses.replace(cfg, risk=dataclasses.replace(
            cfg.risk, daily_loss_soft_pct=D("0.05"), daily_loss_hard_pct=D("0.1")))
        self.rules = {SYM: SymbolRules(SYM, "S00", "USDT", amount_precision=4, price_precision=2)}
        self.clock = MockClock(T + 5)
        self.broker = MockWallex(self.rules.values(), clock=self.clock, balances={"USDT": D(10000)},
                                 maker_rate=self.cfg.maker_rate, taker_rate=self.cfg.taker_rate)
        self.feed = ManualFeed(self.cfg, [SYM])
        self.store = Store(tmp_path / "race.db", clock=self.clock)
        self.ks, self.risk, self.oms = build_stack(self.store, self.broker, self.rules, self.cfg, self.clock)
        self.bot = Bot(self.feed, _Engine(), _Hold(), size_position, self.oms, self.risk, self.store, self.ks,
                       self.cfg, self.clock)
        self.feed.bars[SYM] = _bars(T - B)
        self._price(D("100.00"))
        self.bot.tick()
        assert SYM in self.bot.positions, "rig: the bot did not enter"
        self.held = self.store.latest_positions()[SYM].quantity
        self.gstore = Store(tmp_path / "race.db", clock=self.clock)
        sleep = self.clock.advance if guardian_sleep_advances else (lambda s: None)
        self.guardian = build_guardian(self.gstore, self.broker, self.rules, self.cfg, clock=self.clock,
                                       sleep=sleep)
        self.guardian.tick()             # SOD / HWM at the entry price
        self.sell_submits: list[str] = []
        orig = self.broker.submit_order

        def counting(**kw):
            if kw["side"] is Side.SELL:
                self.sell_submits.append(kw["client_id"])
            return orig(**kw)
        self.broker.submit_order = counting
        self.clock.advance(60)
        self._price(self.bot.positions[SYM].current_stop - D("1.00"))

    def _price(self, bid):
        self.feed.set_quote(SYM, str(bid), str(bid + D("0.02")), self.clock())
        self.broker.set_price(SYM, bid, bid + D("0.02"))

    def guardian_tick(self):
        q = self.feed.quotes[SYM]
        self._price(q.bid)
        self.guardian._last_fresh = {SYM: self.clock()}
        return self.guardian.tick()

    def finish(self, rounds=4):
        for _ in range(rounds):
            self.clock.advance(15)
            self._price(self.feed.quotes[SYM].bid)
            self.bot.tick()
            self.guardian_tick()

    def effective_sells(self):
        sells = [r for r in self.store.intents_in_states(frozenset(S)) if r.intent.side is Side.SELL]
        return [r for r in sells if self.store.fill_totals(r.intent.client_id).quantity > 0]

    def assert_one_sell(self):
        assert len(self.sell_submits) == 1, f"{len(self.sell_submits)} SELLs reached the broker"
        sold = sum((o.filled_qty for o in self.broker.orders.values() if o.side is Side.SELL), D(0))
        assert len(self.effective_sells()) == 1
        assert sold <= self.held and self.held - sold < D("0.0001")


def _hook(obj, name, when, action):
    """Run `action` once, right after obj.name(...) returns for a call where when(args) holds."""
    orig = getattr(obj, name)
    fired = []

    def wrapped(*a, **k):
        out = orig(*a, **k)
        if not fired and when(a, k):
            fired.append(True)
            action()
        return out
    setattr(obj, name, wrapped)
    return fired


def _is_sell(a, k):
    intent = a[0] if a else k.get("intent")
    return isinstance(intent, OrderIntent) and intent.side is Side.SELL


@pytest.mark.parametrize("advances", [True, False])
def test_guardian_inside_bot_risk_transaction_cannot_act(tmp_path, advances):
    """(a) The regression: the guardian ticking between the bot's risk decision and its state
    change used to see a PROPOSED exit, ignore it and sell the full position; the bot then sent a
    second full-size SELL. Now that window is inside one write transaction: the guardian sees no
    decision row and cannot write until the bot's transaction commits."""
    rig = Rig(tmp_path, advances)
    rig.gstore._conn.execute("PRAGMA busy_timeout=0")   # fail fast instead of waiting 5 s on the lock
    seen = {}

    def guardian_inside():
        rec = [r for r in rig.gstore.intents_in_states(frozenset(S)) if r.intent.side is Side.SELL][0]
        seen["state"] = rec.state
        seen["decisions"] = [d for d in rig.gstore.rows("risk_decisions") if d["client_id"] == rec.intent.client_id]
        try:
            rig.guardian_tick()
        except sqlite3.OperationalError as e:
            seen["guardian"] = str(e)
    fired = _hook(rig.risk, "check", _is_sell, guardian_inside)
    rig.bot.tick()
    assert fired
    assert seen["state"] is S.PROPOSED and seen["decisions"] == []   # nothing observable mid-decision
    assert "locked" in seen.get("guardian", "")
    rig.finish()
    rig.assert_one_sell()


@pytest.mark.parametrize("advances", [True, False])
def test_guardian_between_bot_append_and_risk(tmp_path, advances):
    """(b) The bot's exit is PROPOSED (committed row) but its risk transaction has not run yet. The
    guardian treats the young PROPOSED SELL as ambiguous and waits; if the bot stays suspended past
    the commit window the guardian may sell, and the bot's risk transaction then rejects its own
    exit (the guardian's SELL is committed / the ledger is flat)."""
    rig = Rig(tmp_path, advances)
    fired = _hook(rig.store, "append_intent", _is_sell, rig.guardian_tick)
    rig.bot.tick()
    assert fired
    rig.finish()
    rig.assert_one_sell()


def test_guardian_between_bot_approval_and_broker_submit(tmp_path):
    """RISK_APPROVED (after the transaction, before the broker call): ambiguous for the guardian."""
    rig = Rig(tmp_path)
    fired = []
    orig = rig.broker.submit_order

    def submit(**kw):
        if kw["side"] is Side.SELL and not fired:
            fired.append(True)
            rig.guardian_tick()
        return orig(**kw)
    rig.broker.submit_order = submit
    rig.bot.tick()
    assert fired
    rig.finish()
    rig.assert_one_sell()


@pytest.mark.parametrize("point", ["append_intent", "risk_tx"])
def test_bot_inside_guardian_flatten(tmp_path, point):
    """The mirror image: the bot ticks while the guardian's flatten exit is mid-submit."""
    rig = Rig(tmp_path)
    rig.store._conn.execute("PRAGMA busy_timeout=0")
    fired = (_hook(rig.gstore, "append_intent", _is_sell, rig.bot.tick) if point == "append_intent"
             else _hook(rig.guardian.risk, "check", _is_sell, rig.bot.tick))
    rig.guardian_tick()
    assert fired
    rig.finish()
    rig.assert_one_sell()


# ---------------------------------------------------------------------------- unit level (b)
def _account():
    return AccountState(D(10000), D(10000), D(10000), "2023-11-14", D(10000))


def _exit(n, qty="5.0000"):
    return OrderIntent(SYM, Side.SELL, OrderType.MARKET, D(qty), None, f"x:race:{n}", reduce_only=True)


def test_young_proposed_sell_is_committed_for_risk(tmp_path):
    rig = Rig(tmp_path)
    full = rig.held.quantize(D("0.0001"), rounding="ROUND_DOWN")
    first = OrderIntent(SYM, Side.SELL, OrderType.MARKET, full, None, "x:race:a", reduce_only=True)
    rig.store.append_intent(first, ts=rig.clock())           # another actor, not yet risk-checked
    second = OrderIntent(SYM, Side.SELL, OrderType.MARKET, full, None, "x:race:b", reduce_only=True)
    res = rig.oms.submit(second, None, _account())
    assert res.state is S.REJECTED and "exceeds_position" in res.decision.reasons
    assert not rig.sell_submits
    # the earlier one is not blocked by the later (rejected) one
    assert rig.oms.submit(first, None, _account()).state is S.FILLED
    assert len(rig.sell_submits) == 1


def test_later_or_stale_proposed_sell_is_not_committed(tmp_path):
    rig = Rig(tmp_path)
    full = rig.held.quantize(D("0.0001"), rounding="ROUND_DOWN")
    stale = OrderIntent(SYM, Side.SELL, OrderType.MARKET, full, None, "x:race:old", reduce_only=True)
    rig.store.append_intent(stale, ts=rig.clock() - PROPOSED_COMMIT_WINDOW_S)   # crash leftover
    mine = OrderIntent(SYM, Side.SELL, OrderType.MARKET, full, None, "x:race:new", reduce_only=True)
    rig.store.append_intent(mine, ts=rig.clock())
    later = OrderIntent(SYM, Side.SELL, OrderType.LIMIT, full, D("1.00"), "x:race:later", reduce_only=True)
    rig.store.append_intent(later, ts=rig.clock())
    assert rig.oms.submit(mine, None, _account()).state is S.FILLED


def test_flatten_waits_for_young_proposed_and_ignores_stale(tmp_path):
    rig = Rig(tmp_path, guardian_sleep_advances=False)
    full = rig.held.quantize(D("0.0001"), rounding="ROUND_DOWN")
    young = OrderIntent(SYM, Side.SELL, OrderType.MARKET, full, None, "x:race:young", reduce_only=True)
    rig.store.append_intent(young, ts=rig.clock())
    fc = FlattenConfig(unknown_timeout_s=5.0, poll_interval_s=1.0)
    g = rig.guardian
    res = flatten_all(rig.gstore, rig.broker, g.oms, g.ks, cause="t1", hard=True, config=fc,
                      clock=rig.clock, sleep=lambda s: None)
    assert not rig.sell_submits and res.symbols[SYM].status == "needs_review"
    # once the PROPOSED is older than the window it is a leftover: flatten proceeds
    rig.clock.advance(PROPOSED_COMMIT_WINDOW_S)
    res = flatten_all(rig.gstore, rig.broker, g.oms, g.ks, cause="t2", hard=True, config=fc,
                      clock=rig.clock, sleep=lambda s: None)
    assert len(rig.sell_submits) == 1 and res.symbols[SYM].status in ("flat", "dust")


def test_monitor_in_flight_grace_matches_commit_window():
    assert GuardianConfig().in_flight_grace_s == PROPOSED_COMMIT_WINDOW_S


def test_future_dated_proposed_beyond_skew_is_not_young():
    """race2/race3: a PROPOSED written before a backward clock step is dated in the future. Within
    the 5 s skew it still counts as young (an actor may be mid-submit); beyond it, it is a leftover
    that neither blocks exits nor counts as committed."""
    from wallexbot.oms import PROPOSED_FUTURE_SKEW_S, is_young_proposed
    from wallexbot.store import IntentRecord
    i = OrderIntent(SYM, Side.SELL, OrderType.MARKET, D("1.0000"), None, "x:race:f", reduce_only=True)

    def rec(created, state=S.PROPOSED):
        return IntentRecord(i, state, created, created)
    now = 1_000_000.0
    assert is_young_proposed(rec(now), now) and is_young_proposed(rec(now - 29), now)
    assert not is_young_proposed(rec(now - PROPOSED_COMMIT_WINDOW_S), now)
    assert is_young_proposed(rec(now + PROPOSED_FUTURE_SKEW_S), now)
    assert not is_young_proposed(rec(now + PROPOSED_FUTURE_SKEW_S + 0.001), now)
    assert not is_young_proposed(rec(now + 3600), now)
    assert not is_young_proposed(rec(float("nan")), now)
    assert not is_young_proposed(rec(now, S.RISK_APPROVED), now)


def test_flatten_ignores_future_dated_leftover_proposed(tmp_path):
    rig = Rig(tmp_path, guardian_sleep_advances=False)
    full = rig.held.quantize(D("0.0001"), rounding="ROUND_DOWN")
    ghost = OrderIntent(SYM, Side.SELL, OrderType.MARKET, full, None, "x:race:ghost", reduce_only=True)
    rig.store.append_intent(ghost, ts=rig.clock() + 120)        # written before a 120 s backward step
    g = rig.guardian
    res = flatten_all(rig.gstore, rig.broker, g.oms, g.ks, cause="t3", hard=True,
                      config=FlattenConfig(unknown_timeout_s=5.0, poll_interval_s=1.0),
                      clock=rig.clock, sleep=lambda s: None)
    assert len(rig.sell_submits) == 1 and res.symbols[SYM].status in ("flat", "dust")
