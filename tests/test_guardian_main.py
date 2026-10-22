"""Guardian loop: never crashes on a bad tick, N failures engage the kill switch, graceful stop,
own Store connection, and a CLI that refuses anything but mock/shadow."""
import subprocess
import sys
from decimal import Decimal

import pytest

from wallexbot.guardian.main import StopFlag, build_guardian, main, run_guardian, run_loop
from wallexbot.store import Store

D = Decimal
SYM = "BTCUSDT"


@pytest.fixture
def gstore(rig):
    s = Store(rig.store.path, clock=rig.clock)
    yield s
    s.close()


def fresh(rig):
    rig.broker.set_price(SYM, D("60000.00"), D("60010.00"))
    rig.broker.set_price("ETHUSDT", D("3000.00"), D("3001.00"))
    rig.store.heartbeat("strategy")


def test_failed_ticks_are_logged_and_n_in_a_row_engage_the_kill_switch(rig, gstore, monkeypatch):
    g = build_guardian(gstore, rig.broker, rig.rules, rig.cfg, clock=rig.clock)

    def down():
        raise TimeoutError("balances endpoint down")

    monkeypatch.setattr(rig.broker, "balances", down)
    out = run_loop(g, sleep=rig.clock.advance, interval_s=1, max_ticks=5, max_consecutive_failures=3,
                   before_tick=lambda: fresh(rig))
    assert out.ticks == 5 and out.failed_ticks == 5 and out.engaged_for_failures
    ks = [r for r in gstore.rows("killswitch_events")]
    assert len(ks) == 1 and ks[0]["actor"] == "guardian" and "3 consecutive tick failures" in ks[0]["reason"]
    assert sum(1 for r in gstore.rows("errors") if r["source"] == "guardian.loop") == 5


def test_transient_failures_reset_the_counter(rig, gstore, monkeypatch):
    g = build_guardian(gstore, rig.broker, rig.rules, rig.cfg, clock=rig.clock)
    real, calls = rig.broker.balances, {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] % 3:          # fail, fail, ok, fail, fail, ok ...
            raise TimeoutError("blip")
        return real()

    monkeypatch.setattr(rig.broker, "balances", flaky)
    out = run_loop(g, sleep=rig.clock.advance, interval_s=1, max_ticks=9, max_consecutive_failures=3,
                   before_tick=lambda: fresh(rig))
    assert out.failed_ticks == 6 and not out.engaged_for_failures
    assert not rig.ks.is_engaged()


def test_graceful_stop(rig, gstore):
    g = build_guardian(gstore, rig.broker, rig.rules, rig.cfg, clock=rig.clock)
    stop = StopFlag()
    seen = []

    def on_tick(rep):
        seen.append(rep)
        if len(seen) == 2:
            stop.set()

    out = run_loop(g, sleep=rig.clock.advance, stop=stop, before_tick=lambda: fresh(rig), on_tick=on_tick)
    assert out.ticks == 2 and out.failed_ticks == 0


def test_run_guardian_uses_its_own_connection_and_closes_it(rig):
    out = run_guardian(rig.store.path, rig.broker, rig.rules, rig.cfg, clock=rig.clock,
                       sleep=rig.clock.advance, max_ticks=2, before_tick=lambda: fresh(rig))
    assert out.ticks == 2 and out.failed_ticks == 0
    # the strategy-side connection sees the guardian's committed rows
    assert [r["actor"] for r in rig.store.rows("audit_events") if r["kind"] == "tick"] == ["guardian"]


def test_startup_recovers_guardian_intents(rig, gstore):
    from wallexbot.types import IntentState, OrderIntent, OrderType, Side
    mine = OrderIntent(SYM, Side.SELL, OrderType.MARKET, D("0.001000"), None, "gflat:2025-01-01-hard:BTCUSDT:0",
                       reduce_only=True)
    rig.store.append_intent(mine)
    rig.store.set_intent_state(mine.client_id, IntentState.RISK_APPROVED)
    g = build_guardian(gstore, rig.broker, rig.rules, rig.cfg, clock=rig.clock)
    run_loop(g, sleep=rig.clock.advance, max_ticks=1, before_tick=lambda: fresh(rig))
    # recovered to unknown_pending, then reconcile resolves it (never sent -> rejected after grace)
    assert gstore.get_intent(mine.client_id).state in (IntentState.UNKNOWN_PENDING, IntentState.REJECTED)


@pytest.mark.parametrize("mode", ["live", "paper", "LIVE", ""])
def test_cli_refuses_modes_other_than_mock_and_shadow(tmp_path, mode, capsys):
    assert main(["--db", str(tmp_path / "g.db"), "--mode", mode, "--ticks", "1"]) == 2
    assert "refused" in capsys.readouterr().err
    assert not (tmp_path / "g.db").exists()


@pytest.mark.parametrize("mode", ["mock", "shadow"])
def test_cli_mock_and_shadow_run(tmp_path, mode, capsys):
    db = tmp_path / "g.db"
    assert main(["--db", str(db), "--mode", mode, "--demo-db", "--ticks", "2", "--interval", "0"]) == 0
    assert "guardian stopped: ticks=2 failed=0" in capsys.readouterr().out
    s = Store(db)
    try:
        assert {r["actor"] for r in s.rows("audit_events")} == {"guardian"}
    finally:
        s.close()


def test_module_entry_point_refuses_live(tmp_path):
    r = subprocess.run([sys.executable, "-m", "wallexbot.guardian.main", "--db", str(tmp_path / "g.db"),
                        "--mode", "live"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "refused" in r.stderr
