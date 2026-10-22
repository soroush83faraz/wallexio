"""Held position + dead feed (verifier v6/test_c.py).

With stale_action=hold (the default) a held position whose quote froze (timestamp stops) or whose
quote call raises was ignored indefinitely: no kill switch, one early error row at most, and
deploy/healthcheck.py went green again once its 15-minute alert window passed. Now:
- the guardian re-emits a guardian.alert error row every 5 min while any held / ordered symbol has
  no fresh quote for > stale_action_after_s (120 s), and lists them in the tick audit event
  ("held_stale");
- the healthcheck fails on the LATEST guardian tick event reporting held_stale (and on the alerts);
- stale_action is selectable (--stale-action hold|flatten on `bot` and `guardian`); with flatten
  and a raising quote the exit is priced from the broker's order book, else the guardian's last
  known quote, else the attempt fails into needs_review + a guardian.alert (never silent).
"""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from wallexbot.guardian import main as guardian_main
from wallexbot.guardian.main import build_guardian
from wallexbot.guardian.monitor import STALE_ALERT_EVERY_S, GuardianConfig
from wallexbot.store import Store
from wallexbot.types import IntentState, OrderIntent, OrderType, Side

S = IntentState
SYM = "BTCUSDT"
ROOT = Path(__file__).resolve().parents[1]


def _load_healthcheck():
    spec = importlib.util.spec_from_file_location("deploy_healthcheck_held", ROOT / "deploy" / "healthcheck.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load_healthcheck()


def health(rig):
    args = hc.argparse.Namespace(db=str(rig.store.path), max_heartbeat_age_s=120.0, alert_window_min=15.0,
                                 allow_killswitch=False, recorder_dir=None, max_recorder_age_s=300.0)
    return hc.run(args, now=rig.clock())


@pytest.fixture
def gstore(rig):
    s = Store(rig.store.path, clock=rig.clock)
    yield s
    s.close()


def make(rig, gstore, **kw):
    return build_guardian(gstore, rig.broker, rig.rules, rig.cfg, GuardianConfig(**kw), clock=rig.clock,
                          sleep=rig.clock.advance, monotonic=rig.clock)


def buy(rig):
    res = rig.submit(OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.010000"), None, "e1"))
    assert res.state is S.FILLED


def tick(rig, g, seconds=15):
    rig.clock.advance(seconds)
    rig.broker.set_price("ETHUSDT", D("3000"), D("3001"))      # the other symbol stays fresh
    rig.store.heartbeat("strategy")
    rig.store.heartbeat("guardian")
    return g.tick()


def break_quote(rig, how):
    """frozen: the book stays but its timestamp stops; raises: quote() raises for SYM."""
    if how == "raises":
        orig = rig.broker.quote

        def quote(sym):
            if sym == SYM:
                raise KeyError("delisted")
            return orig(sym)
        rig.broker.quote = quote


def stale_alerts(gstore):
    return [r for r in gstore.rows("errors") if r["source"] == "guardian.alert"
            and "no fresh quote for held/ordered" in r["message"]]


def latest_tick(gstore):
    return gstore.audit_events("guardian", "tick")[-1]["payload"]


def sells(rig):
    return [o for o in rig.broker.orders.values() if o.side is Side.SELL]


@pytest.mark.parametrize("how", ["frozen", "raises"])
def test_hold_keeps_alerting_and_the_healthcheck_stays_red(rig, gstore, how):
    buy(rig)
    g = make(rig, gstore)                                   # default stale_action=hold
    tick(rig, g, 0)
    break_quote(rig, how)
    codes = []
    for i in range(80):                                     # 20 minutes, a tick every 15 s
        tick(rig, g)
        if (i + 1) * 15 > 135:
            codes.append(health(rig)[0])
    assert sells(rig) == [] and not rig.ks.is_engaged()     # hold: the position is kept
    assert set(codes) == {1}                                # never green while it lasts
    code, st = health(rig)
    assert st["held_stale"] == [SYM]
    assert any("held/ordered BTCUSDT without a fresh quote (stale_action=hold)" in p for p in st["problems"])
    alerts = stale_alerts(gstore)
    gaps = [b["ts"] - a["ts"] for a, b in zip(alerts, alerts[1:])]
    assert len(alerts) == 4 and all(g_ == STALE_ALERT_EVERY_S for g_ in gaps)   # every 5 min, deduplicated
    assert alerts[0]["payload"].count("held_feed_stale") == 1
    tp = latest_tick(gstore)
    assert tp["held_stale"] == [SYM] and tp["stale_action"] == "hold"


def test_feed_recovery_clears_the_condition_and_the_probe_recovers(rig, gstore):
    buy(rig)
    g = make(rig, gstore)
    tick(rig, g, 0)
    for _ in range(12):                                     # 3 minutes frozen
        tick(rig, g)
    assert latest_tick(gstore)["held_stale"] == [SYM] and len(stale_alerts(gstore)) == 1
    rig.broker.set_price(SYM, D("60000.00"), D("60010.00"))    # fresh again
    tick(rig, g, 1)
    assert latest_tick(gstore)["held_stale"] == []
    code, st = health(rig)
    assert code == 1 and st["held_stale"] == []             # only the recent alert row remains
    for _ in range(64):                                     # 16 min of fresh feed: alert window passes
        rig.broker.set_price(SYM, D("60000.00"), D("60010.00"))
        tick(rig, g)
    code, st = health(rig)
    assert code == 0, st["problems"]
    assert len(stale_alerts(gstore)) == 1


@pytest.mark.parametrize("how", ["frozen", "raises"])
def test_flatten_policy_sells_even_without_a_quote(rig, gstore, how):
    buy(rig)
    g = make(rig, gstore, stale_action="flatten")
    tick(rig, g, 0)
    break_quote(rig, how)
    reps = [tick(rig, g) for _ in range(12)]
    assert any("flatten:stale" in r.actions for r in reps)
    (o,) = sells(rig)
    assert o.type is OrderType.LIMIT
    assert rig.broker.balances().get("BTC") is None or rig.broker.balances()["BTC"].free == 0
    if how == "raises":
        (fb,) = gstore.audit_events("guardian", "flatten_quote_fallback")[:1]
        assert fb["payload"]["source"] == "order_book"
    assert health(rig)[0] == 1                              # the flatten latched the kill switch


def test_flatten_with_no_book_falls_back_to_the_last_known_quote(rig, gstore):
    buy(rig)
    g = make(rig, gstore, stale_action="flatten")
    tick(rig, g, 0)                                         # a valid quote is seen once
    break_quote(rig, "raises")

    def no_book(sym):
        raise LookupError("no book")
    rig.broker.depth = no_book
    for _ in range(12):
        tick(rig, g)
    assert len(sells(rig)) == 1
    fb = gstore.audit_events("guardian", "flatten_quote_fallback")
    assert fb and fb[0]["payload"]["source"] == "last_known_quote"


def test_flatten_with_no_price_at_all_needs_review_with_an_alert(rig, gstore):
    buy(rig)
    break_quote(rig, "raises")                              # broken before the guardian ever saw a quote

    def no_book(sym):
        raise LookupError("no book")
    rig.broker.depth = no_book
    g = make(rig, gstore, stale_action="flatten")
    reps = [tick(rig, g) for _ in range(12)]
    flat = [r.flatten for r in reps if r.flatten is not None]
    assert flat and flat[-1].needs_review and not flat[-1].flat
    assert sells(rig) == []
    msgs = [r["message"] for r in gstore.rows("errors") if r["source"] == "guardian.alert"]
    assert any("flatten needs operator review: BTCUSDT" in m for m in msgs)
    assert any("no fresh quote for held/ordered BTCUSDT" in m for m in msgs)
    assert health(rig)[0] == 1


# ---------------------------------------------------------------------------- healthcheck unit
def _tick_event(store, ts, held_stale, stale_action="hold"):
    store.append_audit_event("guardian", "tick", {"held_stale": held_stale, "stale_action": stale_action,
                                                  "conditions": [], "actions": []}, ts=ts)


def _ok_base(store, now):
    store.heartbeat("strategy", ts=now - 5)
    store.heartbeat("guardian", ts=now - 5)


def test_healthcheck_reads_only_the_latest_tick_of_the_current_run(rig):
    now = rig.clock()
    st = rig.store
    st.append_audit_event("bot", "startup", {"run": {"mode": "mock", "profile": "strict",
                                                     "guardian": {"enabled": True}}}, ts=now - 3600)
    _tick_event(st, now - 3000, [SYM])
    _ok_base(st, now)
    code, out = health(rig)
    assert code == 1 and out["held_stale"] == [SYM]
    _tick_event(st, now - 2000, [])                         # the latest tick says clear
    assert health(rig)[0] == 0
    _tick_event(st, now - 1000, [SYM])
    assert health(rig)[0] == 1
    st.append_audit_event("bot", "startup", {"run": {"mode": "mock", "profile": "strict",
                                                     "guardian": {"enabled": True}}}, ts=now - 60)
    code, out = health(rig)                                 # a new run: the old tick is not its state
    assert code == 0 and out["held_stale"] == []
    st.append_audit_event("bot", "startup", {"run": {"mode": "mock", "profile": "strict",
                                                     "guardian": {"enabled": False}}}, ts=now - 30)
    _tick_event(st, now - 20, [SYM])
    assert "held_stale" not in health(rig)[1]               # guardian off in this run: not checked


# ---------------------------------------------------------------------------- CLI
@pytest.mark.parametrize("value", ["panic", ""])
def test_stale_action_is_validated_on_both_clis(tmp_path, value):
    from wallexbot.bot import main as bot_main
    with pytest.raises(SystemExit) as e:
        bot_main.main(["--db", str(tmp_path / "b.db"), "--mode", "mock", "--stale-action", value])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        guardian_main.main(["--db", str(tmp_path / "g.db"), "--mode", "mock", "--demo-db", "--stale-action", value])
    assert e.value.code == 2


@pytest.mark.parametrize("value", [None, "hold", "flatten"])
def test_bot_cli_passes_stale_action_to_the_in_process_guardian(tmp_path, monkeypatch, value):
    from wallexbot.bot import main as bot_main
    seen = {}

    def fake_feed_loop(mode, db, cfg, **kw):
        seen.update(kw)
        return {}
    monkeypatch.setattr(bot_main, "run_feed_loop", fake_feed_loop)
    argv = ["--db", str(tmp_path / "b.db"), "--mode", "mock", "--max-ticks", "1"]
    assert bot_main.main(argv + ([] if value is None else ["--stale-action", value])) == 0
    assert seen["stale_action"] == (value or "hold")


def test_make_guardian_and_run_info_carry_stale_action(rig):
    from wallexbot.bot import main as bot_main
    gd = bot_main.make_guardian(True, rig.store.path, rig.broker, rig.rules, rig.cfg, rig.clock,
                                rig.clock.advance, 15.0, None, monotonic=rig.clock, stale_action="flatten")
    try:
        assert gd.guardian.cfg.stale_action == "flatten"
    finally:
        gd.close()
    assert bot_main._run_info("mock", "strict", True, 15.0, "flatten")["guardian"]["stale_action"] == "flatten"


def test_guardian_cli_passes_stale_action(tmp_path, monkeypatch):
    seen = {}

    def fake_run(db, broker, rules, cfg, gcfg, **kw):
        seen["gcfg"] = gcfg
        return guardian_main.RunSummary()
    monkeypatch.setattr(guardian_main, "run_guardian", fake_run)
    assert guardian_main.main(["--db", str(tmp_path / "g.db"), "--mode", "mock", "--demo-db", "--ticks", "1",
                               "--stale-action", "flatten"]) == 0
    assert seen["gcfg"].stale_action == "flatten"
