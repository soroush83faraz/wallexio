"""Persisted bot state: append-only, latest wins, survives a new Store connection."""
from decimal import Decimal

import pytest

from wallexbot.bot.state import BotState, EntryPlan, ExitRequest, TrackedPosition, decision_log
from wallexbot.store import Store, StoreError
from wallexbot.strategy.cooldown import CooldownTracker

D = Decimal


def _tp(**kw):
    base = dict(symbol="BTCUSDT", key="sig_" + "a" * 24, origin="signal", entry_bar_ts=900, entry_price=D("100.5"),
                initial_stop=D("99"), current_stop=D("99"), breakout_level=D("100"),
                highest_high_since_entry=D("100.5"))
    base.update(kw)
    return TrackedPosition(**base)


def test_latest_row_wins_and_reload_is_exact(tmp_path):
    s = Store(tmp_path / "a.db")
    st = BotState(s)
    st.save_position(_tp(), 1.0)
    later = _tp(current_stop=D("100.1234567890123456789"), bars_held=3, last_bar_ts=2700, partial_taken=True,
                atr=D("0.75"), exit_request=ExitRequest("partial_take_profit", D("1.5"), 5.0))
    st.save_position(later, 2.0)
    s.close()
    loaded = BotState(Store(tmp_path / "a.db")).load()
    assert loaded.positions == {"BTCUSDT": later}
    assert len([r for r in Store(tmp_path / "a.db").audit_events(actor="bot", kind="position_state")]) == 2


def test_close_drops_only_the_matching_position(tmp_path):
    s = Store(tmp_path / "a.db")
    st = BotState(s)
    a = _tp()
    st.save_position(a, 1.0)
    st.close_position(_tp(key="sig_" + "b" * 24), "stale key", D("1"), 2.0)   # different trade: ignored
    assert st.load().positions == {"BTCUSDT": a}
    st.close_position(a, "stop", D("-1"), 3.0)
    assert st.load().positions == {}
    b = _tp(key="sig_" + "c" * 24)
    st.save_position(b, 4.0)                                    # a later trade on the same symbol
    assert st.load().positions == {"BTCUSDT": b}


def test_plans_cooldown_and_bar_markers(tmp_path):
    s = Store(tmp_path / "a.db")
    st = BotState(s)
    plan = EntryPlan("sig_" + "d" * 24, "BTCUSDT", "wb_" + "0" * 32, 900, 1800, D("101"), D("99"), D("100"),
                     D("0.8"), D("0.75"), 10.0)
    st.add_plan(plan, 10.0)
    cd = CooldownTracker(900)
    cd.record_exit("BTCUSDT", 1800, profitable=False, stopped_out=True, breakout_level=D("100"))
    st.save_cooldown(cd.to_dict(), 11.0)
    st.mark_bars({"BTCUSDT": 900, "ETHUSDT": 900}, 12.0)
    st.mark_bars({"BTCUSDT": 1800}, 13.0)
    ld = st.load()
    assert ld.open_plans == {plan.signal_id: plan} and ld.planned == {plan.signal_id}
    assert CooldownTracker.from_dict(ld.cooldown).to_dict() == cd.to_dict()
    assert ld.bars_done == {"BTCUSDT": 1800, "ETHUSDT": 900}
    st.resolve_plan(plan, "filled", 14.0)
    ld = st.load()
    assert ld.open_plans == {} and ld.planned == {plan.signal_id}   # never planned again


def test_state_rows_are_append_only(tmp_path):
    s = Store(tmp_path / "a.db")
    BotState(s).save_position(_tp(), 1.0)
    with pytest.raises(Exception):
        s._conn.execute("UPDATE audit_events SET payload='{}'")
    with pytest.raises(Exception):
        s._conn.execute("DELETE FROM audit_events")
    with pytest.raises(StoreError):
        s.append_audit_event("bot", "position state with spaces", {})


def test_decision_log_is_scoped_to_one_trade(tmp_path):
    s = Store(tmp_path / "a.db")
    st = BotState(s)
    st.decision("abstain", {"symbol": "BTCUSDT", "reason": "no_signal"}, 1.0)
    st.decision("signal", {"symbol": "BTCUSDT", "signal_id": "sig_x"}, 2.0)
    st.decision("signal", {"symbol": "ETHUSDT", "signal_id": "sig_y"}, 3.0)
    log = decision_log(s, "sig_x")
    assert [e["payload"]["signal_id"] for e in log["events"]] == ["sig_x"]
    assert log["intents"] == [] and log["fills"] == []
