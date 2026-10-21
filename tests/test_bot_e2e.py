"""End-to-end paper runs: S1 signal -> sizing -> RiskEngine -> OMS -> MockWallex -> exit, offline."""
from decimal import Decimal

from test_bot_helpers import ENTRY_TS, LOSER, SIGNAL_BAR, STEP, SYM, T0, WINNER, Rig, series

from wallexbot.bot.state import decision_log
from wallexbot.types import IntentState, Side

END_WIN = T0 + (SIGNAL_BAR + 1 + len(WINNER)) * STEP + 60
END_LOSE = T0 + (SIGNAL_BAR + 1 + len(LOSER)) * STEP + 60


def _run(tmp_path, cont, end):
    rig = Rig(tmp_path, series(cont))
    reps = rig.run_until(end)
    return rig, reps


def test_winner_round_trip_through_risk_oms_mock(tmp_path):
    rig, reps = _run(tmp_path, WINNER, END_WIN)
    assert not any(r.errors for r in reps)
    intents = rig.intents()
    entries = [r for r in intents if r.intent.side is Side.BUY]
    exits = [r for r in intents if r.intent.side is Side.SELL]
    assert len(entries) == 1 and entries[0].state is IntentState.FILLED
    sid = entries[0].intent.signal_id
    assert sid.startswith("sig_") and entries[0].created_at == ENTRY_TS   # entered on the bar close
    assert exits and all(r.state is IntentState.FILLED and r.intent.reduce_only for r in exits)
    assert [r.intent.signal_id for r in exits] == [f"x:{sid}:{n}" for n in range(len(exits))]
    # partial take-profit at +1R, then the rest on the exit policy's signal
    reasons = [e["payload"]["reason"] for e in rig.events("exit_submitted")]
    assert reasons[0] == "partial_take_profit" and len(reasons) == 2
    assert rig.bot.positions == {}                                     # closed (only sub-step dust left)
    pos = rig.store.latest_positions()[SYM]
    assert pos.quantity < Decimal("0.0001") and pos.realized_pnl > 0
    # exactly one broker order per intent, none outside the OMS
    assert rig.broker.submit_calls == len(intents) == 3
    assert set(rig.broker.orders) == {r.intent.client_id for r in intents}
    rig.close()


def test_audit_trail_rebuilds_the_trade(tmp_path):
    rig, _ = _run(tmp_path, WINNER, END_WIN)
    sid = next(r.intent.signal_id for r in rig.intents(Side.BUY))
    log = decision_log(rig.store, sid)
    assert len(log["signals"]) == 1
    payload = log["signals"][0]["payload"]
    assert "breakout" in payload and "cost_gate" in payload                # rule-by-rule reasons
    kinds = [e["kind"] for e in log["events"]]
    for k in ("signal", "entry_plan", "entry_submitted", "position_opened", "exit_decision", "exit_requested",
              "exit_submitted", "exit_done", "position_closed"):
        assert k in kinds, k
    assert kinds.index("entry_plan") < kinds.index("entry_submitted")    # plan persisted before the order path
    cids = {r.intent.client_id for r in log["intents"]}
    assert len(cids) == 3
    assert {r["client_id"] for r in log["risk_decisions"]} == cids
    assert all(r["approved"] == 1 for r in log["risk_decisions"])
    assert {r["client_id"] for r in log["fills"]} == cids
    assert {r["to_state"] for r in log["transitions"]} >= {"proposed", "risk_approved", "submitted", "filled"}
    closed = [e for e in log["events"] if e["kind"] == "position_closed"][0]["payload"]
    assert Decimal(closed["pnl"]) > 0
    rig.close()


def test_loser_exits_and_cooldown_is_persisted(tmp_path):
    rig, reps = _run(tmp_path, LOSER, END_LOSE)
    assert not any(r.errors for r in reps)
    exits = rig.intents(Side.SELL)
    assert len(exits) == 1 and exits[0].state is IntentState.FILLED
    closed = rig.events("position_closed")[-1]["payload"]
    assert Decimal(closed["pnl"]) < 0
    cd = rig.events("cooldown_state")[-1]["payload"]
    assert SYM in cd["exits"]
    assert rig.broker.submit_calls == 2
    rig.close()


def test_heartbeat_every_tick_and_abstentions_logged(tmp_path):
    rig = Rig(tmp_path, series(WINNER))
    reps = rig.run_until(ENTRY_TS - 60)
    assert len(rig.store.audit_events(actor="strategy", kind="heartbeat")) == len(reps)
    abst = rig.events("abstain")
    assert abst and {e["payload"]["reason"] for e in abst} == {"no_signal"}
    assert all("failed" in e["payload"] for e in abst)
    assert rig.intents() == []
    rig.close()


def test_runs_are_deterministic(tmp_path):
    a, _ = _run(tmp_path / "a", WINNER, END_WIN)
    b, _ = _run(tmp_path / "b", WINNER, END_WIN)
    key = [(r.intent.client_id, r.state) for r in a.intents()]
    assert key == [(r.intent.client_id, r.state) for r in b.intents()]
    assert a.store.latest_positions()[SYM].realized_pnl == b.store.latest_positions()[SYM].realized_pnl
    a.close()
    b.close()
