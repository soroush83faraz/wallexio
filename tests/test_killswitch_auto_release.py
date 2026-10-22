"""Kill-switch auto-release: ONLY the guardian's own transient ('auto:') engagement, only once clear.

Rules (guardian/monitor.py, risk/killswitch.py): the guardian engages the switch for a transient
condition (strategy heartbeat lost) with reason 'auto:<conditions>' and actor 'guardian'. While that
row is the LATEST killswitch row and no freezing condition is seen for 3 consecutive ticks spanning
at least 60 s, the guardian disengages it (actor 'guardian', audited 'release_kill_switch'). Any
other engagement (operator, bot, latching guardian reasons, tick failures, flatten), or one written
after the guardian's, is never released by the guardian.
"""
import sqlite3
from decimal import Decimal

import pytest

from wallexbot.guardian.main import InProcessGuardian, build_guardian
from wallexbot.guardian.monitor import GuardianConfig
from wallexbot.risk.killswitch import AUTO_PREFIX, KillSwitch
from wallexbot.store import Store
from wallexbot.types import IntentState, OrderIntent, OrderType, Side

D = Decimal
SYM = "BTCUSDT"
S = IntentState
BID, ASK = D("60000.00"), D("60010.00")


@pytest.fixture
def gstore(rig):
    s = Store(rig.store.path, clock=rig.clock)
    yield s
    s.close()


@pytest.fixture
def g(rig, gstore):
    # The simulated clock is the monotonic clock too (no wall-clock jumps unless a test makes one).
    return build_guardian(gstore, rig.broker, rig.rules, rig.cfg, GuardianConfig(heartbeat_timeout_s=60),
                          clock=rig.clock, sleep=rig.clock.advance, monotonic=rig.clock)


def step(rig, g, seconds=15, alive=True):
    rig.clock.advance(seconds)
    rig.broker.set_price(SYM, BID, ASK)
    rig.broker.set_price("ETHUSDT", D("3000.00"), D("3001.00"))
    if alive:
        rig.store.heartbeat("strategy")
    return g.tick()


def lose_heartbeat(rig, g):
    step(rig, g, 1)
    for _ in range(5):
        rep = step(rig, g, 15, alive=False)
    ev = rig.store.latest_killswitch()
    assert ev.engaged and ev.actor == "guardian" and ev.reason == AUTO_PREFIX + "heartbeat_lost"
    return rep


def releases(gstore):
    return gstore.audit_events("guardian", "release_kill_switch")


def test_heartbeat_engagement_is_released_after_3_clear_ticks_and_60s(rig, gstore, g):
    lose_heartbeat(rig, g)
    reps = [step(rig, g, 15) for _ in range(4)]          # clear at t0, +15, +30, +45: 4 ticks, 45 s
    assert all("release_kill_switch" not in r.actions for r in reps) and rig.ks.is_engaged()
    rep = step(rig, g, 15)                                # +60 s: released
    assert "release_kill_switch" in rep.actions and not rig.ks.is_engaged()
    ev = rig.store.latest_killswitch()
    assert ev.engaged is False and ev.actor == "guardian" and "auto-release" in ev.reason
    (audit,) = releases(gstore)
    assert audit["actor"] == "guardian" and audit["payload"]["released"] == "auto:heartbeat_lost"
    assert audit["payload"]["clear_ticks"] == 5


def test_tick_count_is_required_too(rig, g):
    """60 s of clear time in only two ticks is not enough."""
    lose_heartbeat(rig, g)
    step(rig, g, 15)
    assert "release_kill_switch" not in step(rig, g, 120).actions and rig.ks.is_engaged()
    assert "release_kill_switch" in step(rig, g, 15).actions


def test_flicker_restarts_the_clear_window(rig, g):
    lose_heartbeat(rig, g)
    for _ in range(3):
        step(rig, g, 15)
    step(rig, g, 70, alive=False)                         # heartbeat lost again: window restarts
    for _ in range(4):
        assert "release_kill_switch" not in step(rig, g, 15).actions
    assert "release_kill_switch" in step(rig, g, 15).actions


def test_operator_engagement_after_the_guardian_one_is_never_released(rig, g):
    lose_heartbeat(rig, g)
    KillSwitch(rig.store).engage("operator: investigating", actor="operator", ts=rig.clock())
    for _ in range(20):
        assert "release_kill_switch" not in step(rig, g, 15).actions
    ev = rig.store.latest_killswitch()
    assert ev.engaged and ev.actor == "operator"


def test_operator_engagement_is_never_released(rig, g):
    KillSwitch(rig.store).engage("auto:looks-transient", actor="operator", ts=rig.clock())
    for _ in range(20):
        assert "release_kill_switch" not in step(rig, g, 15).actions
    assert rig.ks.is_engaged()


@pytest.mark.parametrize("actor, reason", [("bot", "bot: 5 consecutive tick failures"),
                                           ("guardian", "guardian: 3 consecutive tick failures"),
                                           ("guardian", "guardian:flatten:2023-11-14-hard:drawdown_hard")])
def test_non_transient_engagements_are_never_released(rig, g, actor, reason):
    KillSwitch(rig.store).engage(reason, actor=actor, ts=rig.clock())
    for _ in range(20):
        assert "release_kill_switch" not in step(rig, g, 15).actions
    assert rig.ks.is_engaged() and rig.store.latest_killswitch().reason == reason


def test_latching_condition_supersedes_the_transient_engagement(rig, gstore, g):
    """A balance mismatch appears while only the transient engagement holds the switch: a new,
    latching row is written, so the heartbeat recovering can no longer release the switch."""
    rig.submit(OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.010000"), None, "e1"))
    lose_heartbeat(rig, g)
    rig.broker._free["BTC"] -= D("0.004000")
    step(rig, g, 15)
    rep = step(rig, g, 15)
    assert "engage_kill_switch" in rep.actions
    ev = rig.store.latest_killswitch()
    assert ev.engaged and not ev.reason.startswith(AUTO_PREFIX) and "balance_mismatch" in ev.reason
    rig.broker._free["BTC"] += D("0.004000")             # even once everything is clear again
    for _ in range(20):
        assert "release_kill_switch" not in step(rig, g, 15).actions
    assert rig.ks.is_engaged() and not releases(gstore)


def test_soft_breach_while_transient_is_latching(rig, gstore, g):
    rig.submit(OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.010000"), None, "e2"))
    lose_heartbeat(rig, g)
    gstore.append_equity_hwm(D("10900"))                  # drawdown ~8.3 %: soft
    rep = step(rig, g, 15)
    assert "drawdown_soft" in rep.conditions and "engage_kill_switch" in rep.actions
    assert "drawdown_soft" in rig.store.latest_killswitch().reason


def test_hard_breach_flatten_supersedes_the_transient_engagement(rig, gstore, g):
    rig.submit(OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.010000"), None, "e3"))
    lose_heartbeat(rig, g)
    gstore.append_equity_hwm(D("20000"))
    rep = step(rig, g, 15)
    assert "flatten:hard" in rep.actions
    assert not rig.store.latest_killswitch().reason.startswith(AUTO_PREFIX)
    for _ in range(10):
        assert "release_kill_switch" not in step(rig, g, 15).actions


def test_bot_failure_engagement_supersedes_the_transient_one(rig, g):
    lose_heartbeat(rig, g)
    assert KillSwitch(rig.store).engage_unless_held("bot: 5 consecutive tick failures", actor="bot",
                                                    ts=rig.clock())
    assert not KillSwitch(rig.store).engage_unless_held("bot: 6 consecutive tick failures", actor="bot",
                                                        ts=rig.clock())
    for _ in range(10):
        assert "release_kill_switch" not in step(rig, g, 15).actions
    assert rig.store.latest_killswitch().actor == "bot"


def test_backward_clock_step_restarts_the_clear_window(rig, g):
    lose_heartbeat(rig, g)
    step(rig, g, 15)
    step(rig, g, 15)
    rig.clock.advance(-600)
    for _ in range(4):                                    # re-anchored: 4 ticks / 45 s on the new clock
        assert "release_kill_switch" not in step(rig, g, 15).actions
    assert "release_kill_switch" in step(rig, g, 15).actions


def test_guardian_restart_keeps_a_transient_engagement_releasable(rig, gstore, g):
    lose_heartbeat(rig, g)
    g2 = build_guardian(gstore, rig.broker, rig.rules, rig.cfg, GuardianConfig(), clock=rig.clock,
                        sleep=rig.clock.advance, monotonic=rig.clock)
    for _ in range(4):
        assert "release_kill_switch" not in step(rig, g2, 15).actions
    assert "release_kill_switch" in step(rig, g2, 15).actions


def test_inprocess_tick_failure_engagement_is_not_transient(rig):
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("broker down")
    gd = InProcessGuardian(rig.store.path, Broken(), rig.rules, rig.cfg, clock=rig.clock,
                           sleep=rig.clock.advance, max_consecutive_failures=2)
    gd.tick()
    gd.tick()
    ev = rig.store.latest_killswitch()
    assert ev.engaged and not ev.reason.startswith(AUTO_PREFIX)
    gd.close()


@pytest.mark.parametrize("kw", [{"auto_release_clear_s": 59}, {"auto_release_clear_ticks": 0},
                                {"auto_release_clear_s": float("nan")}])
def test_release_thresholds_cannot_be_weakened(kw):
    with pytest.raises(ValueError):
        GuardianConfig(**kw)


# ---------------------------------------------------------------------------- v6: TOCTOU and clocks
def test_operator_row_written_between_check_and_release_is_never_released(rig, gstore, g):
    """Verifier v6/test_b.py: the transient check, the reason read and the disengage were separate
    statements, so an operator row written by ANOTHER connection right after the guardian's check
    was released by the guardian. The release is now a compare-and-set on the observed row id
    inside one IMMEDIATE transaction."""
    lose_heartbeat(rig, g)
    for _ in range(4):
        step(rig, g, 15)
    op = sqlite3.connect(rig.store.path, timeout=5)
    real = g.ks.latest_transient

    def check_then_operator_writes():
        ev = real()
        if ev is not None:            # forced interleaving: right after the guardian looked
            op.execute("INSERT INTO killswitch_events(ts, engaged, reason, actor) VALUES "
                       "(?, 1, 'operator: manual stop', 'operator')", (rig.clock(),))
            op.commit()
        return ev
    g.ks.latest_transient = check_then_operator_writes
    rep = step(rig, g, 15)                                # the tick that would have released
    op.close()
    ev = rig.store.latest_killswitch()
    assert "release_kill_switch" not in rep.actions
    assert ev.engaged and ev.actor == "operator" and ev.reason == "operator: manual stop"
    assert not releases(gstore)
    (refused,) = gstore.audit_events("guardian", "release_kill_switch_refused")
    assert refused["payload"]["observed"] == "auto:heartbeat_lost"
    g.ks.latest_transient = real
    for _ in range(10):                                   # and never later either
        assert "release_kill_switch" not in step(rig, g, 15).actions
    assert rig.store.latest_killswitch().actor == "operator"


def test_compare_and_set_release_checks_row_id_actor_and_reason(rig):
    ks = KillSwitch(rig.store)
    ks.engage("auto:heartbeat_lost", actor="guardian", ts=rig.clock())
    first = rig.store.latest_killswitch()
    ks.engage("auto:heartbeat_lost", actor="guardian", ts=rig.clock())     # a newer row
    assert not ks.release_transient(first.id, "stale id", ts=rig.clock())
    assert rig.store.latest_killswitch().engaged
    for actor in ("operator", "bot"):                                      # forged 'auto:' reasons
        ks.engage("auto:heartbeat_lost", actor=actor, ts=rig.clock())
        assert not ks.release_transient(rig.store.latest_killswitch().id, "forged", ts=rig.clock())
        assert rig.store.latest_killswitch().engaged
    ks.engage("auto:heartbeat_lost", actor="guardian", ts=rig.clock())
    assert ks.release_transient(rig.store.latest_killswitch().id, "ok", ts=rig.clock())
    assert rig.store.latest_killswitch().engaged is False


@pytest.mark.parametrize("actor", ["operator", "bot"])
def test_forged_auto_reason_by_operator_or_bot_is_never_released(rig, g, actor):
    step(rig, g, 1)
    KillSwitch(rig.store).engage("auto:heartbeat_lost", actor=actor, ts=rig.clock())
    for _ in range(20):
        assert "release_kill_switch" not in step(rig, g, 15).actions
    ev = rig.store.latest_killswitch()
    assert ev.engaged and ev.actor == actor


class Mono:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_forward_wall_clock_jump_does_not_shorten_the_clear_window(rig, gstore):
    """Verifier v6: ticks +1 s, +1 s, then a +300 s wall-clock jump released the switch after ~2
    real seconds. The 60 s floor now holds on the monotonic clock as well."""
    mono = Mono()
    g = build_guardian(gstore, rig.broker, rig.rules, rig.cfg, GuardianConfig(heartbeat_timeout_s=60),
                       clock=rig.clock, sleep=rig.clock.advance, monotonic=mono)

    def tick(wall, real, alive=True):
        mono.t += real
        return step(rig, g, wall, alive)
    tick(1, 1)
    for _ in range(5):
        tick(15, 15, alive=False)
    assert rig.ks.held_by_transient()
    out = [("release_kill_switch" in tick(w, r).actions) for w, r in ((1, 1), (1, 1), (300, 1), (1, 1))]
    assert out == [False, False, False, False] and rig.ks.is_engaged()
    for _ in range(3):                                    # 4 + 45 = 49 s of monotonic time: still held
        assert "release_kill_switch" not in tick(15, 15).actions
    assert "release_kill_switch" in tick(15, 15).actions  # 64 s monotonic, > 60 s wall: released
    (audit,) = releases(gstore)
    assert float(audit["payload"]["clear_mono_s"]) >= 60


def test_default_monotonic_is_real_time(rig, gstore):
    """Without an injected monotonic clock the guardian uses time.monotonic: simulated wall-clock
    seconds alone can never satisfy the 60 s floor."""
    g = build_guardian(gstore, rig.broker, rig.rules, rig.cfg, GuardianConfig(heartbeat_timeout_s=60),
                       clock=rig.clock, sleep=rig.clock.advance)
    lose_heartbeat(rig, g)
    for _ in range(10):
        assert "release_kill_switch" not in step(rig, g, 300).actions
    assert rig.ks.is_engaged()
