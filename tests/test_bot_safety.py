"""Fail-safe behaviour of the loop: stale data, kill switch, software stops, failures, never raising."""
import dataclasses
from decimal import Decimal

from test_bot_helpers import ENTRY_TS, LOSER, SIGNAL_BAR, STEP, SYM, T0, Rig, cheap_config, series
from test_bot_restart import manual_rig

from wallexbot.bot.loop import BotParams
from wallexbot.types import IntentState, Side

D = Decimal


def test_stale_quote_blocks_entries(tmp_path):
    rig, feed = manual_rig(tmp_path)
    feed.set_quote(SYM, "102.49", "102.51", ENTRY_TS - 31)     # older than max_data_age_s (30 s)
    rep = rig.tick()
    assert rig.intents() == [] and rig.broker.submit_calls == 0
    assert rig.events("abstain")[-1]["payload"]["reason"] == "stale_feed"
    assert not rep.errors
    rig.close()


def test_stale_bars_block_entries(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.clock.advance(3 * STEP)                                # bars now lag the clock by 3 bars
    feed.set_quote(SYM, "102.49", "102.51", rig.clock())
    rig.tick()
    assert rig.intents() == []
    assert rig.events("abstain")[-1]["payload"]["reason"] == "bars_stale"
    rig.close()


def test_stale_feed_still_exits_on_the_software_stop(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.tick()
    assert SYM in rig.bot.positions
    feed.refresh_error = RuntimeError("feed down")             # nothing refreshes any more
    rig.clock.advance(120)
    feed.set_quote(SYM, "101.00", "101.04", ENTRY_TS + 5)      # last known (stale) quote is below the stop
    rig.broker.set_price(SYM, D("101.00"), D("101.04"))
    rep = rig.tick()
    assert "feed:RuntimeError" in rep.errors
    ex = rig.intents(Side.SELL)
    assert len(ex) == 1 and ex[0].state is IntentState.FILLED and ex[0].intent.reduce_only
    rig.close()


def test_kill_switch_blocks_entries(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.ks.engage("operator test", actor="operator")
    rig.tick()
    assert rig.intents() == [] and rig.broker.submit_calls == 0
    assert rig.events("abstain")[-1]["payload"]["reason"] == "kill_switch"
    rig.close()


def test_kill_switch_does_not_block_exits(tmp_path):
    rig = Rig(tmp_path, series(LOSER))
    rig.run_until(ENTRY_TS + 60)
    assert SYM in rig.bot.positions
    rig.ks.engage("guardian soft breach", actor="guardian")
    rig.run_until(T0 + (SIGNAL_BAR + 1 + len(LOSER)) * STEP)
    ex = rig.intents(Side.SELL)
    assert len(ex) == 1 and ex[0].state is IntentState.FILLED
    assert rig.bot.positions == {}
    rig.close()


def test_software_stop_between_bars(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.tick()
    stop = rig.bot.positions[SYM].current_stop
    for k in range(1, 4):                                      # above the stop: nothing happens
        rig.clock.advance(60)
        feed.set_quote(SYM, str(stop + D("0.05")), str(stop + D("0.07")), rig.clock())
        rig.broker.set_price(SYM, stop + D("0.05"), stop + D("0.07"))
        assert not rig.tick().new_bar
    assert rig.intents(Side.SELL) == []
    rig.clock.advance(17)                                      # mid-bar, off any minute boundary
    feed.set_quote(SYM, str(stop), str(stop + D("0.02")), rig.clock())
    rig.broker.set_price(SYM, stop, stop + D("0.02"))
    rep = rig.tick()
    assert not rep.new_bar
    ex = rig.intents(Side.SELL)
    assert len(ex) == 1 and ex[0].state is IntentState.FILLED and ex[0].created_at == rig.clock()
    req = rig.events("exit_requested")[-1]["payload"]
    assert req["reason"] == "stop" and D(req["bid"]) == stop
    rig.tick()
    assert rig.bot.positions == {}
    rig.close()


class _Broken:
    def __getattr__(self, name):
        raise RuntimeError(f"broken {name}")


class _RaisingFeed:
    def symbols(self):
        raise RuntimeError("symbols")

    def track(self, symbols):
        raise RuntimeError("track")

    def refresh(self, now):
        raise RuntimeError("refresh")

    def closed_bars(self, symbol, n):
        raise RuntimeError("bars")

    def quote(self, symbol):
        raise RuntimeError("quote")

    def depth(self, symbol):
        raise RuntimeError("depth")

    def market_context(self, symbol, notional=None):
        raise RuntimeError("ctx")


def test_tick_never_raises(tmp_path):
    rig = Rig(tmp_path, feed=_RaisingFeed(), start=ENTRY_TS)
    for _ in range(3):
        rep = rig.tick()
        assert rep.errors
    # a position under management while everything around it breaks
    rig2, feed = manual_rig(tmp_path / "b")
    rig2.tick()
    rig2.bot.exit_policy = _Broken()
    rig2.bot.engine = _Broken()
    rig2.oms.broker = _Broken()
    rig2.bot.feed = _RaisingFeed()
    for _ in range(3):
        rig2.clock.advance(STEP)
        rep = rig2.tick()
        assert rep.errors
    rig2.bot.clock = _Broken()                                 # even the clock
    rig2.bot.tick()
    rig.close()
    rig2.close()


def test_consecutive_failures_engage_the_kill_switch_and_the_loop_continues(tmp_path):
    rig, feed = manual_rig(tmp_path, params=BotParams(max_consecutive_failures=3))
    feed.refresh_error = RuntimeError("upstream 503")
    reps = [rig.tick() for _ in range(2)]
    assert not rig.ks.is_engaged()
    reps.append(rig.tick())
    assert reps[-1].engaged_kill_switch and rig.ks.is_engaged()
    ev = rig.store.latest_killswitch()
    assert ev.engaged and ev.actor == "bot" and "3 consecutive" in ev.reason
    assert rig.intents() == []                                 # every failing tick abstained from entries
    feed.refresh_error = None
    rep = rig.tick()
    assert not rep.errors and rep.consecutive_failures == 0
    assert rig.intents() == [] and rig.events("abstain")[-1]["payload"]["reason"] == "kill_switch"
    assert len(rig.store.audit_events(actor="strategy", kind="heartbeat")) == 4
    rig.close()


def test_degraded_tick_takes_no_entries(tmp_path):
    rig, feed = manual_rig(tmp_path)
    feed.refresh_error = RuntimeError("one bad refresh")
    rep = rig.tick()
    assert rep.errors and rig.intents() == []
    assert rig.events("abstain")[-1]["payload"]["reason"] == "tick_degraded"
    rig.close()


def test_invalidated_signal_is_not_entered(tmp_path):
    rig, feed = manual_rig(tmp_path)
    feed.set_quote(SYM, "103.40", "103.42", ENTRY_TS)          # ask > signal close + 0.5 ATR
    rig.tick()
    assert rig.intents() == []
    assert rig.events("entry_skipped")[-1]["payload"]["reason"] == "invalidated_price"
    rig.close()


def test_one_order_per_symbol_and_entry_ioc(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.broker.set_price(SYM, D("102.80"), D("102.90"))        # exchange ask above our limit: entry rests
    rig.tick()
    ent = rig.intents(Side.BUY)
    assert len(ent) == 1 and ent[0].state is IntentState.SUBMITTED
    rig.clock.advance(15)
    rig.bot.bars_done.clear()                                  # even re-evaluating the bar adds nothing
    feed.set_quote(SYM, "102.49", "102.51", rig.clock())
    rig.tick()
    assert len(rig.intents(Side.BUY)) == 1
    rig.clock.advance(10)                                      # > 20 s after submit: emulated IOC cancel
    rig.tick()
    assert rig.intents(Side.BUY)[0].state is IntentState.CANCELED
    assert rig.events("entry_ioc_canceled")
    assert rig.bot.positions == {} and rig.broker.submit_calls == 1
    rig.close()


def test_entry_limit_respects_tolerance(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.tick()
    ent = rig.intents(Side.BUY)[0].intent
    assert ent.type.value == "LIMIT" and D("102.51") <= ent.price <= D("102.51") * D("1.001")


def test_low_balance_is_a_sizing_abstention(tmp_path):
    rig, feed = manual_rig(tmp_path, balance="3")
    rig.tick()
    assert rig.intents() == []
    skip = rig.events("entry_skipped")[-1]["payload"]
    assert skip["reason"] == "sizing" and "below_min_notional" in skip["reasons"]
    rig.close()


def test_params_validation():
    import pytest
    with pytest.raises(ValueError):
        BotParams(entry_tolerance_bps=D("11"))
    with pytest.raises(ValueError):
        BotParams(max_consecutive_failures=0)
    assert dataclasses.replace(cheap_config(), top_n=1).top_n == 1
