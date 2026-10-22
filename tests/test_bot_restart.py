"""Crash / restart safety: persisted stop state, no duplicate entries or exits."""
import dataclasses
from decimal import Decimal

import pytest
from test_bot_helpers import (ENTRY_TS, LOSER, SIGNAL_BAR, STEP, SYM, T0, WINNER, ManualFeed, Rig, cheap_config,
                              series)

import wallexbot.bot.loop as loop_mod
from wallexbot.store import Store
from wallexbot.types import IntentState, Side

D = Decimal
END_WIN = T0 + (SIGNAL_BAR + 1 + len(WINNER)) * STEP + 60


def manual_rig(tmp_path, cont=LOSER, **kw):
    """Bot on a hand-driven feed: bars up to the signal bar, fresh quote, entry fills at ENTRY_TS."""
    cfg = kw.pop("config", None) or cheap_config()
    feed = ManualFeed(cfg)
    rig = Rig(tmp_path, feed=feed, start=ENTRY_TS, config=cfg, **kw)
    feed.bars[SYM] = series(cont)[:SIGNAL_BAR + 1]
    feed.set_quote(SYM, "102.49", "102.51", ENTRY_TS)
    rig.broker.set_price(SYM, D("102.49"), D("102.51"))
    return rig, feed


def test_restart_mid_position_restores_stops_and_matches_uninterrupted_run(tmp_path):
    ref = Rig(tmp_path / "ref", series(WINNER))
    ref.run_until(END_WIN)
    ref_trades = [(r.intent.client_id, r.state) for r in ref.intents()]

    rig = Rig(tmp_path / "crash", series(WINNER))
    rig.run_until(T0 + (SIGNAL_BAR + 5) * STEP + 30)          # partial taken, stop trailed up
    before = rig.bot.positions[SYM]
    assert before.partial_taken and before.current_stop > before.initial_stop
    rig.restart()
    rep = rig.tick()
    assert "startup" in rep.actions and not rep.errors
    assert rig.bot.positions[SYM] == before                    # every stop field restored exactly
    assert rig.events("orphan_adopted") == []
    rig.clock.advance(60)
    rig.run_until(END_WIN)
    assert [(r.intent.client_id, r.state) for r in rig.intents()] == ref_trades
    assert rig.store.latest_positions()[SYM].realized_pnl == ref.store.latest_positions()[SYM].realized_pnl
    assert rig.broker.submit_calls == ref.broker.submit_calls == 3
    ref.close()
    rig.close()


def test_ambiguous_exit_then_crash_never_sends_a_second_exit(tmp_path):
    rig = Rig(tmp_path, series(LOSER))
    rig.run_until(ENTRY_TS + 60)
    assert len(rig.intents(Side.BUY)) == 1
    rig.broker.queue_fault("timeout_after_accept")            # the exit fills, but the reply is lost
    rig.run_until(T0 + (SIGNAL_BAR + 3) * STEP + 60)
    ex = rig.intents(Side.SELL)
    assert len(ex) == 1 and ex[0].state is IntentState.UNKNOWN_PENDING
    rig.restart()
    rig.run_until(rig.clock() + 600)
    ex = rig.intents(Side.SELL)
    assert len(ex) == 1 and ex[0].state is IntentState.FILLED  # resolved by reconcile, never resent
    assert rig.broker.submit_calls == 2
    assert rig.bot.positions == {}
    rig.close()


def test_software_stop_then_crash_with_resting_exit_escalates_once(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rep = rig.tick()
    assert any(a.startswith("entry:") for a in rep.actions) and SYM in rig.bot.positions
    stop = rig.bot.positions[SYM].current_stop
    # mid-bar (no new bar): the quote falls through the stop; the exchange book is lower still, so the
    # marketable-at-the-quote LIMIT exit does not fill and rests
    rig.clock.advance(300)
    feed.set_quote(SYM, "101.00", "101.04", rig.clock())
    rig.broker.set_price(SYM, D("100.00"), D("100.04"))
    assert D("101.00") <= stop
    rep = rig.tick()
    assert not rep.new_bar
    ex = rig.intents(Side.SELL)
    assert len(ex) == 1 and ex[0].intent.type.value == "LIMIT" and ex[0].state is IntentState.SUBMITTED
    assert rig.events("exit_requested")[-1]["payload"]["reason"] == "stop"
    rig.restart(advance=60)                                    # crash with the exit resting
    feed.set_quote(SYM, "101.00", "101.04", rig.clock())
    rep = rig.tick()
    ex = rig.intents(Side.SELL)
    assert [(r.intent.type.value, r.state) for r in ex] == [("LIMIT", IntentState.CANCELED),
                                                             ("MARKET", IntentState.FILLED)]
    log = rig.store.rows("intent_state_log")
    canceled_at = next(r["id"] for r in log if r["client_id"] == ex[0].intent.client_id and r["to_state"] == "canceled")
    proposed_at = next(r["id"] for r in log if r["client_id"] == ex[1].intent.client_id and r["to_state"] == "proposed")
    assert canceled_at < proposed_at                           # never two live exits
    rig.run_until(rig.clock() + 300)
    assert rig.broker.submit_calls == 3 and rig.bot.positions == {}
    rig.close()


def test_same_bar_processed_twice_and_after_restart_sends_nothing_new(tmp_path):
    cfg = dataclasses.replace(cheap_config(), max_spread_bps=D("1"))   # risk rejects the entry
    rig, feed = manual_rig(tmp_path, config=cfg)
    rig.tick()
    ent = rig.intents(Side.BUY)
    assert len(ent) == 1 and ent[0].state is IntentState.REJECTED and rig.broker.submit_calls == 0
    rd = rig.store.rows("risk_decisions")
    assert rd[-1]["approved"] == 0 and "spread_too_wide" in rd[-1]["reasons"]
    assert rig.events("entry_rejected")[-1]["payload"]["risk_reasons"] == ["spread_too_wide"]
    # the bar marker is lost: the same bar is evaluated again -> same signal_id -> not planned again
    rig.bot.bars_done.clear()
    rig.clock.advance(60)
    feed.set_quote(SYM, "102.49", "102.51", rig.clock())
    rig.tick()
    assert rig.events("abstain")[-1]["payload"]["reason"] == "already_planned"
    rig.restart(advance=60)
    feed.set_quote(SYM, "102.49", "102.51", rig.clock())
    rig.tick()                                                 # startup reloads the planned signal ids
    rig.bot.bars_done.clear()
    rig.tick()
    assert rig.events("abstain")[-1]["payload"]["reason"] == "already_planned"
    assert len(rig.intents()) == 1 and rig.broker.submit_calls == 0
    rig.close()


def test_crash_between_plan_and_order_path_loses_the_signal_not_doubles_it(tmp_path, monkeypatch):
    rig, feed = manual_rig(tmp_path)
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise RuntimeError("process died here")

    monkeypatch.setattr(loop_mod, "route_to_oms", boom)
    rep = rig.tick()
    assert any(e.startswith("entry:") for e in rep.errors) and len(calls) == 1
    assert rig.events("entry_plan") and rig.intents() == []
    monkeypatch.undo()
    rig.restart(advance=60)
    feed.set_quote(SYM, "102.49", "102.51", rig.clock())
    rig.tick()                                                 # startup: plan reloaded, then resolved
    rig.bot.bars_done.clear()                                  # force the same bar to be evaluated again
    rig.tick()
    resolved = rig.events("entry_resolved")
    assert resolved and resolved[-1]["payload"]["outcome"] == "never_submitted"
    assert rig.events("abstain")[-1]["payload"]["reason"] == "already_planned"
    assert rig.broker.submit_calls == 0 and rig.intents() == []
    rig.close()


@pytest.mark.parametrize("with_bars", [True, False])
def test_orphan_position_gets_conservative_stop_or_review(tmp_path, with_bars):
    """A ledger position the bot has no state for (state lost) is protected, never ignored."""
    rig, feed = manual_rig(tmp_path)
    rig.tick()                                                 # entry + state
    # lost bot state: a database holding only the ledger position (no bot events, no intents)
    other = tmp_path / "other"
    other.mkdir()
    s2 = Store(other / "bot.db", clock=rig.clock)
    pos = rig.store.latest_positions()[SYM]
    s2.append_position_snapshot(pos)
    s2.close()
    rig.store.close()
    rig.tmp_path = other
    if not with_bars:
        feed.bars[SYM] = []
    rig._open()
    rig.clock.advance(60)
    feed.set_quote(SYM, "102.49", "102.51", rig.clock())
    rig.tick()
    tp = rig.bot.positions[SYM]
    assert tp.origin == "orphan"
    if with_bars:
        assert not tp.needs_review and tp.current_stop < pos.avg_price and tp.atr is not None
    else:
        assert tp.needs_review and tp.review_reason == "orphan_without_atr"
        assert tp.current_stop == (pos.avg_price * D("0.95")).quantize(D("0.01"), rounding="ROUND_DOWN")
    assert rig.events("orphan_adopted")
    # the orphan is still protected by the software stop
    rig.clock.advance(60)
    feed.set_quote(SYM, str(tp.current_stop - 1), str(tp.current_stop - D("0.9")), rig.clock())
    rig.broker.set_price(SYM, tp.current_stop - 1, tp.current_stop - D("0.9"))
    rig.tick()
    assert [r.intent.reduce_only for r in rig.intents(Side.SELL)] == [True]
    rig.close()


@pytest.mark.parametrize("gap, resumed", [(15, True), (60, False)])
def test_entry_left_proposed_by_a_store_error_is_resumed_or_dropped_next_poll(tmp_path, gap, resumed):
    """OMS.submit raised (database is locked) after the entry intent was appended: the next poll
    resumes the SAME intent while it is younger than entry_ioc_s (20 s), else rejects it (it was
    never sent); either way the symbol is not blocked forever and nothing is sent twice."""
    import sqlite3
    rig = Rig(tmp_path)
    real, fired = rig.oms.submit, []

    def flaky(intent, *a, **k):
        if intent.side is Side.BUY and not fired:
            fired.append(intent.client_id)
            rig.store.append_intent(intent, ts=rig.clock())
            raise sqlite3.OperationalError("database is locked")
        return real(intent, *a, **k)
    rig.oms.submit = flaky
    while not fired:
        rig.tick()
        rig.clock.advance(60)
    rig.clock.advance(gap - 60)
    rep = rig.tick()
    (cid,) = fired
    state, S = rig.store.get_intent(cid).state, IntentState
    if resumed:
        assert f"entry_resumed:{SYM}:filled" in rep.actions and state is S.FILLED
        assert sum(o.client_id == cid for o in rig.broker.orders.values()) == 1
    else:
        assert f"settled:{SYM}:rejected" in rep.actions and state is S.REJECTED
        assert all(o.client_id != cid for o in rig.broker.orders.values())
