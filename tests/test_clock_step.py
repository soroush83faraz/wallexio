"""Backward clock step / future-dated data: fail closed, and timers re-anchor instead of stalling.

Each test steps the clock BACK (or dates data in the future) and checks that
- future-dated data is invalid, never fresh (bot bar gate, bot quote freshness, shadow depth);
- a timer on a stored timestamp (OMS not-found grace, OMS stale-entry cancel, bot LIMIT exit
  timeout, feed retry timers) runs one full interval from the step, not until the clock catches up.
"""
from __future__ import annotations

import dataclasses
import math
from decimal import Decimal as D

import pytest
from test_bot_feed import FakeClient
from test_bot_helpers import ManualFeed, cheap_config

from wallexbot.bot import main as bot_main
from wallexbot.bot.feed import FeedParams, PublicFeed
from wallexbot.bot.loop import Bot
from wallexbot.bot.main import build_stack
from wallexbot.clockstep import StepAnchors
from wallexbot.data.candles import Candle
from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.store import Store
from wallexbot.strategy.contract import ExitDecision, Signal
from wallexbot.strategy.sizing import size_position
from wallexbot.types import IntentState, OrderIntent, OrderType, Quote, Side, SymbolRules

S = IntentState
B = 900
T = 1_700_000_100 // B * B
SYM = "S00USDT"
STEP_BACK = 3600.0


# ---------------------------------------------------------------------------- StepAnchors
def test_step_anchors():
    a = StepAnchors()
    assert a.elapsed("k", 100.0, 130.0) == 30.0                    # normal
    assert a.elapsed("f", 1000.0, 100.0) == 0.0                    # future ts: anchored at now
    assert a.elapsed("f", 1000.0, 90.0) == 0.0                     # stepped back again: earlier anchor
    assert a.elapsed("f", 1000.0, 120.0) == 30.0                   # runs from the anchor (90)
    assert a.elapsed("f", 1000.0, 1001.0) == 911.0                 # clock caught up: keeps the anchor
    assert a.elapsed("n", math.nan, 50.0) == 0.0 and a.elapsed("n", math.nan, 80.0) == 30.0
    a.keep_only(["k"])
    assert a.elapsed("f", 1000.0, 100.0) == 0.0                    # forgotten: re-anchored afresh


# ---------------------------------------------------------------------------- bot rig
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


def _bot(tmp_path):
    cfg = cheap_config()
    rules = {SYM: SymbolRules(SYM, "S00", "USDT", amount_precision=4, price_precision=2)}
    clock = MockClock(T + 5)
    broker = MockWallex(rules.values(), clock=clock, balances={"USDT": D(10000)},
                        maker_rate=cfg.maker_rate, taker_rate=cfg.taker_rate)
    feed = ManualFeed(cfg, [SYM])
    store = Store(tmp_path / "c.db", clock=clock)
    ks, risk, oms = build_stack(store, broker, rules, cfg, clock)
    bot = Bot(feed, _Engine(), _Hold(), size_position, oms, risk, store, ks, cfg, clock)
    feed.set_quote(SYM, "100.00", "100.02", clock())
    broker.set_price(SYM, D("100.00"), D("100.02"))
    return bot, feed, broker, store, clock


def _buys(store):
    return [r for r in store.intents_in_states(frozenset(S)) if r.intent.side is Side.BUY]


def test_future_dated_bar_is_not_fresh(tmp_path):
    """bot/loop.py bar-lag gate: a bar not yet closed by the clock (clock stepped back) used to give
    a negative lag and count as fresh."""
    bot, feed, broker, store, clock = _bot(tmp_path)
    feed.bars[SYM] = _bars(T + 4 * B)              # newest "closed" bar opens 4 bars after the clock
    bot.tick()
    assert _buys(store) == []
    ev = [e["payload"] for e in store.audit_events(actor="bot", kind="abstain")]
    assert any(p.get("reason") == "bars_future" for p in ev), ev
    feed.bars[SYM] = _bars(T - B)                  # control: the newest closed bar enters
    bot.tick()
    assert len(_buys(store)) == 1


def test_future_dated_quote_is_not_fresh(tmp_path):
    bot, feed, broker, store, clock = _bot(tmp_path)
    assert not bot._fresh(Quote(SYM, D("100"), D("100.02"), clock() + 1), clock())
    assert not bot._fresh(Quote(SYM, D("100"), D("100.02"), math.nan), clock())
    assert bot._fresh(Quote(SYM, D("100"), D("100.02"), clock()), clock())


def test_exit_limit_timeout_reanchors_after_backward_step(tmp_path):
    bot, feed, broker, store, clock = _bot(tmp_path)
    feed.bars[SYM] = _bars(T - B)
    bot.tick()
    assert SYM in bot.positions
    broker.fault_mode = "rest"                     # the LIMIT exit rests
    stop = bot.positions[SYM].current_stop
    clock.advance(30)
    feed.set_quote(SYM, str(stop - 1), str(stop - D("0.98")), clock())
    bot.tick()
    sells = [r for r in store.intents_in_states({S.SUBMITTED}) if r.intent.side is Side.SELL]
    assert len(sells) == 1 and sells[0].intent.type is OrderType.LIMIT
    clock.advance(-STEP_BACK)                      # the clock steps back an hour
    for _ in range(3):                             # 3 x 6 s > exit_limit_timeout_s (10 s) after the step
        clock.advance(6)
        feed.set_quote(SYM, str(stop - 1), str(stop - D("0.98")), clock())
        bot.tick()
    assert store.get_intent(sells[0].intent.client_id).state is S.CANCELED


# ---------------------------------------------------------------------------- OMS timers
def _buy(sig="sig1", price="60010.00"):
    return OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D("0.010000"), D(price), sig)


def test_unknown_pending_grace_reanchors_after_backward_step(rig):
    rig.broker.queue_fault("timeout_before_accept")      # never created at the exchange
    i = _buy()
    assert rig.submit(i).state is S.UNKNOWN_PENDING
    rig.clock.advance(-STEP_BACK)
    rig.oms.reconcile()                                  # anchors the grace at the new clock
    assert rig.store.get_intent(i.client_id).state is S.UNKNOWN_PENDING
    rig.clock.advance(31)                                # grace (30 s) from the step, not from +1 h
    rig.oms.reconcile()
    assert rig.store.get_intent(i.client_id).state is S.REJECTED


def test_stale_entry_cancel_reanchors_after_backward_step(rig):
    rig.broker.fault_mode = "rest"
    i = _buy(price="59000.00")
    assert rig.submit(i).state is S.SUBMITTED
    rig.clock.advance(-STEP_BACK)
    assert rig.oms.cancel_stale(20) == []
    rig.clock.advance(21)
    assert rig.oms.cancel_stale(20) == [i.client_id]


# ---------------------------------------------------------------------------- feed timers
def test_feed_retry_timers_reanchor_after_backward_step():
    cfg = dataclasses.replace(cheap_config(), top_n=1)
    clock = MockClock(T + 7 * 60 + 13)
    c = FakeClient(clock)
    sym = "AAAUSDT"
    c.ranked = [SymbolRules(sym, "AAA", "USDT", 4, 2)]
    c.books = {sym: ("100", "100.01", "1000")}
    feed = PublicFeed(c, cfg, params=FeedParams(history_bars=8, max_requests_per_refresh=100))
    feed.refresh(clock())
    n_depth = sum(1 for x in c.calls if x[:2] == ("depth", sym))
    clock.advance(-STEP_BACK)
    feed.refresh(clock())                                # re-anchors (one interval from here)
    timers = [feed._universe_ts, feed._universe_try, *feed._depth_try.values(), *feed._candle_try.values()]
    assert all(t <= clock() for t in timers)
    clock.advance(feed.params.poll_interval_s)
    feed.refresh(clock())
    assert sum(1 for x in c.calls if x[:2] == ("depth", sym)) > n_depth, "depth polling stalled"
    assert feed.quote(sym).ts == clock()                 # the cached quote is current again
    # (candles already held beyond the new clock are not re-requested; bars built from them are
    # future-dated, which the bot's bar gate refuses until the clock passes them)


# ---------------------------------------------------------------------------- shadow book source
def test_shadow_depth_source_rejects_future_dated_book():
    clock = MockClock(T)
    c = FakeClient(clock)

    class _Feed:
        def depth(self, sym):
            return c.depth(sym)
    c.books = {SYM: ("100", "100.01", "1000")}
    src = bot_main.fresh_depth_source(_Feed(), clock, max_age_s=5.0)
    assert src(SYM).ts == clock()
    real = clock()
    c.clock = MockClock(real + 60)                       # cached receive time is ahead of the clock
    with pytest.raises(LookupError, match="after the clock"):
        src(SYM)
