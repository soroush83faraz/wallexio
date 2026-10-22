"""Exchange-wide market-data outages are DEGRADED, not failures (Wallex public API outages of
80-180 minutes happen). Before this, every FeedError counted as a tick failure and 5 in a row
latched the bot's kill switch, which only an operator can clear.

- feed errors (FeedError, HTTP / 429 / timeout / bad response) never count toward the kill switch;
  the heartbeat is still written, entries pause and resume on the first good refresh, exits on
  fresh data still happen; a deduplicated feed_down event + an errors row every 5 minutes;
- internal failures still engage the kill switch after 5 in a row (machine-readable reason);
- the tick report / summary expose feed_down_since / feed_outage_s (backward-clock-step safe);
- PublicFeed probes a down API with one request and bounded backoff (12 s doubling to 60 s);
- deploy/healthcheck.py warns during a short outage and fails past --max-feed-outage-s;
- a held position during an outage is the guardian's (stale_action), never a bot kill switch."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_bot_feed import FakeClient, _rules
from test_bot_helpers import ENTRY_TS, RULES, SYM, T0, cheap_config
from test_bot_restart import manual_rig

from wallexbot.bot import main as bot_main
from wallexbot.bot.feed import FeedError, FeedParams, PublicFeed
from wallexbot.bot.loop import FEED_ALERT_EVERY_S, BotParams
from wallexbot.exchange.mock_wallex import MockClock
from wallexbot.exchange.wallex_public import WallexDataError, WallexHTTPError
from wallexbot.guardian.main import build_guardian
from wallexbot.guardian.monitor import GuardianConfig
from wallexbot.store import Store
from wallexbot.types import IntentState, Side

ROOT = Path(__file__).resolve().parents[1]
DOWN = FeedError("every market-data request of this refresh failed: depth:BTCUSDT:OSError")


def _load_healthcheck():
    spec = importlib.util.spec_from_file_location("deploy_healthcheck_feed", ROOT / "deploy" / "healthcheck.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load_healthcheck()


def run(rig, n, step=12.0):
    reps = []
    for _ in range(n):
        reps.append(rig.tick())
        rig.clock.advance(step)
    return reps


def feed_rows(rig):
    return [r for r in rig.store.rows("errors") if r["source"] == "bot.feed_down"]


def ks_engaged(rig):
    ev = rig.store.latest_killswitch()
    return ev is not None and ev.engaged


# ---------------------------------------------------------------------------- the bot
def test_twenty_feed_errors_never_engage_the_kill_switch_and_entries_resume(tmp_path):
    rig, feed = manual_rig(tmp_path)                    # a signal is ready; the first good tick enters
    feed.refresh_error = DOWN
    reps = run(rig, 20)
    assert not ks_engaged(rig) and not any(r.engaged_kill_switch for r in reps)
    assert all(not r.errors and r.degraded == ["feed:FeedError"] and r.consecutive_failures == 0 for r in reps)
    assert len(rig.store.audit_events(actor="strategy", kind="heartbeat")) == 20
    assert rig.intents() == [] and rig.broker.submit_calls == 0
    assert [e["payload"]["reason"] for e in rig.events("abstain")] == ["feed_down"]   # once per bar
    down = rig.events("feed_down")
    assert len(down) == 1 and float(down[0]["payload"]["since"]) == ENTRY_TS        # deduplicated
    assert len(feed_rows(rig)) == 1                                                  # 228 s < 5 min
    assert reps[0].feed_down_since == ENTRY_TS and reps[0].feed_outage_s == 0
    assert reps[-1].feed_down_since == ENTRY_TS and reps[-1].feed_outage_s == 19 * 12
    # the API is back: the very first successful refresh re-enables entries on the same signal
    feed.refresh_error = None
    feed.set_quote(SYM, "102.49", "102.51", rig.clock())
    rig.broker.set_price(SYM, D("102.49"), D("102.51"))
    rep = rig.tick()
    assert "feed_restored" in rep.actions and rep.feed_down_since is None and rep.feed_outage_s is None
    assert not rep.errors and not rep.degraded
    buys = rig.intents(Side.BUY)
    assert len(buys) == 1 and buys[0].state is IntentState.FILLED and SYM in rig.bot.positions
    restored = rig.events("feed_restored")
    assert len(restored) == 1 and float(restored[0]["payload"]["outage_s"]) == 20 * 12
    rig.close()


def test_held_position_with_a_software_stop_exits_when_data_returns(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.tick()
    assert SYM in rig.bot.positions
    stop = rig.bot.positions[SYM].current_stop
    rig.clock.advance(12)
    feed.refresh_error = DOWN                          # last quote stays (above the stop) and ages
    reps = run(rig, 20)
    assert rig.intents(Side.SELL) == [] and not ks_engaged(rig)
    assert all(not r.errors for r in reps) and SYM in rig.bot.positions
    feed.refresh_error = None                          # back, and the market is below the stop
    feed.set_quote(SYM, str(stop - D("0.50")), str(stop - D("0.46")), rig.clock())
    rig.broker.set_price(SYM, stop - D("0.50"), stop - D("0.46"))
    rep = rig.tick()
    ex = rig.intents(Side.SELL)
    assert len(ex) == 1 and ex[0].state is IntentState.FILLED and ex[0].intent.reduce_only
    assert rig.bot.positions == {} and not rep.errors and not ks_engaged(rig)
    rig.close()


@pytest.mark.parametrize("exc", [DOWN, WallexHTTPError("GET failed after 4 attempts: HTTP 429"),
                                 WallexHTTPError("HTTP 503"), WallexDataError("depth: response not successful"),
                                 TimeoutError("timed out"), ConnectionError("reset"), OSError("unreachable")])
def test_transient_market_data_errors_are_degraded(tmp_path, exc):
    rig, feed = manual_rig(tmp_path, params=BotParams(max_consecutive_failures=2))
    feed.refresh_error = exc
    reps = run(rig, 6)
    assert all(not r.errors and r.degraded == [f"feed:{type(exc).__name__}"] for r in reps)
    assert not ks_engaged(rig) and rig.bot.consecutive_failures == 0
    rig.close()


@pytest.mark.parametrize("exc", [RuntimeError("bug"), ValueError("bug"), KeyError("bug")])
def test_other_feed_stage_exceptions_still_count(tmp_path, exc):
    rig, feed = manual_rig(tmp_path, params=BotParams(max_consecutive_failures=2))
    feed.refresh_error = exc
    reps = run(rig, 2)
    assert reps[-1].engaged_kill_switch and ks_engaged(rig) and rig.bot.feed_down_since is None
    rig.close()


def _boom(*a, **k):
    raise RuntimeError("internal bug")


def test_five_internal_failures_engage_with_a_machine_readable_reason(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.tick()                                         # started (startup reconciles too)
    rig.clock.advance(12)
    rig.oms.reconcile = _boom                         # the OMS stage raises every tick
    reps = run(rig, 5)
    assert [r.consecutive_failures for r in reps] == [1, 2, 3, 4, 5]
    assert reps[-1].engaged_kill_switch and not any(r.engaged_kill_switch for r in reps[:-1])
    ev = rig.store.latest_killswitch()
    assert ev.engaged and ev.actor == "bot" and ev.reason == "bot: 5 consecutive internal failures"
    assert "reconcile:RuntimeError" in rig.events("engage_kill_switch")[-1]["payload"]["errors"]
    assert len(rig.store.audit_events(actor="strategy", kind="heartbeat")) == 6
    rig.close()


def test_risk_engine_failures_count_too(tmp_path):
    rig, feed = manual_rig(tmp_path)
    rig.bot._sync = _boom                             # a logic stage raising (patched)
    run(rig, 4)
    assert not ks_engaged(rig)
    run(rig, 1)
    assert ks_engaged(rig) and "internal failures" in rig.store.latest_killswitch().reason
    rig.close()


def test_mixed_sequences(tmp_path):
    # outage ticks neither count nor reset: 3 internal + 10 outage + 2 internal = 5 in a row
    rig, feed = manual_rig(tmp_path / "a")
    for exc, n in ((RuntimeError("bug"), 3), (DOWN, 10), (RuntimeError("bug"), 1)):
        feed.refresh_error = exc
        run(rig, n)
    assert not ks_engaged(rig) and rig.bot.consecutive_failures == 4
    rep = run(rig, 1)[-1]
    assert rep.engaged_kill_switch and rig.store.latest_killswitch().reason == "bot: 5 consecutive internal failures"
    rig.close()
    # a clean tick (feed up) resets the count
    rig, feed = manual_rig(tmp_path / "b")
    feed.refresh_error = RuntimeError("bug")
    run(rig, 4)
    feed.refresh_error = None
    assert run(rig, 1)[-1].consecutive_failures == 0
    feed.refresh_error = RuntimeError("bug")
    run(rig, 4)
    assert not ks_engaged(rig)
    rig.close()
    # an internal failure DURING an outage still counts
    rig, feed = manual_rig(tmp_path / "c")
    rig.tick()
    feed.refresh_error = DOWN
    rig.oms.reconcile = _boom
    reps = run(rig, 5)
    assert all(r.degraded and r.errors for r in reps) and reps[-1].engaged_kill_switch
    rig.close()


def test_outage_duration_and_five_minute_error_rows(tmp_path):
    rig, feed = manual_rig(tmp_path)
    feed.refresh_error = DOWN
    reps = run(rig, 31, step=30.0)                     # 900 s of outage
    rows = feed_rows(rig)
    assert len(rows) == 4                              # at 0, 300, 600 and 900 s
    payloads = [json.loads(r["payload"]) for r in rows]
    assert [float(p["outage_s"]) for p in payloads] == [0, 300, 600, 900]
    assert all(float(p["since"]) == ENTRY_TS for p in payloads) and "market data down for 900s" in rows[-1]["message"]
    assert reps[-1].feed_outage_s == 900 and FEED_ALERT_EVERY_S == 300
    out = bot_main.summarize(rig.store, rig.bot, len(reps))
    assert out["feed_down_since"] == ENTRY_TS and out["feed_outage_s"] == 900 and not out["kill_switch_engaged"]
    rig.close()


def test_backward_clock_step_during_an_outage_does_not_break_the_timers(tmp_path):
    rig, feed = manual_rig(tmp_path)
    feed.refresh_error = DOWN
    run(rig, 3, step=100.0)                            # outage 200 s
    assert rig.bot.feed_outage_s == 200
    rig.clock.t -= 3600 + 100                          # NTP step back an hour
    reps = run(rig, 2, step=100.0)
    assert [r.feed_outage_s for r in reps] == [200, 300]      # never negative, never stalled
    assert all(r.feed_down_since == ENTRY_TS and not r.errors for r in reps)
    assert len(feed_rows(rig)) == 2                    # the 5-minute timer still fires
    feed.refresh_error = None
    rep = rig.tick()
    assert "feed_restored" in rep.actions and float(rig.events("feed_restored")[-1]["payload"]["outage_s"]) == 400
    assert not ks_engaged(rig)
    rig.close()


# ---------------------------------------------------------------------------- held position + guardian
@pytest.mark.parametrize("action", ["hold", "flatten"])
def test_outage_with_a_held_position_is_the_guardians_concern(tmp_path, action):
    rig, feed = manual_rig(tmp_path)
    rig.tick()
    assert SYM in rig.bot.positions
    gstore = Store(rig.store.path, clock=rig.clock)
    g = build_guardian(gstore, rig.broker, RULES, rig.cfg, GuardianConfig(stale_action=action), clock=rig.clock,
                       sleep=rig.clock.advance, monotonic=rig.clock)
    g.tick()
    feed.refresh_error = DOWN                          # neither the bot's feed nor the book moves
    for _ in range(60):                                # 12 minutes
        rig.clock.advance(12)
        assert not rig.tick().errors
        g.tick()
    ev = rig.store.latest_killswitch()
    assert ev is None or ev.actor != "bot"             # the bot never latches anything
    alerts = [r for r in gstore.rows("errors") if r["source"] == "guardian.alert"
              and "no fresh quote for held/ordered" in r["message"]]
    assert len(alerts) >= (2 if action == "hold" else 1)   # every 5 min while held (flatten: until sold)
    tick = gstore.audit_events("guardian", "tick")[-1]["payload"]
    sells = [o for o in rig.broker.orders.values() if o.side is Side.SELL]
    if action == "hold":                               # kept, alerting, entries still open after
        assert tick["held_stale"] == [SYM] and tick["stale_action"] == "hold"
        assert sells == [] and not rig.ks.is_engaged()
    else:                                              # the guardian sold it; the bot did not add exits
        assert sells and all(r.intent.signal_id.startswith("gflat:") for r in rig.intents(Side.SELL))
        assert rig.bot.positions == {}
    assert rig.bot.feed_down_since is not None and rig.bot.consecutive_failures == 0
    gstore.close()
    rig.close()


# ---------------------------------------------------------------------------- PublicFeed probe / backoff
def _public(clock):
    c = FakeClient(clock)
    c.ranked = [_rules(s) for s in ("AAAUSDT", "BBBUSDT")]
    c.books = {s: ("100", "100.01", "1000") for s in ("AAAUSDT", "BBBUSDT")}
    cfg = dataclasses.replace(cheap_config(), top_n=2)
    return c, PublicFeed(c, cfg, params=FeedParams(history_bars=8))


def test_public_feed_probes_a_down_api_with_bounded_backoff():
    clock = MockClock(T0)
    c, feed = _public(clock)
    feed.refresh(clock())
    assert feed.outage_since is None and feed.symbols() == ["AAAUSDT", "BBBUSDT"]
    c.fail = {"AAAUSDT", "BBBUSDT"}
    clock.advance(60)
    with pytest.raises(FeedError):
        feed.refresh(clock())
    assert feed.outage_since == clock()
    probes = []
    for _ in range(40):                                # 40 polls 12 s apart (8 minutes)
        clock.advance(12)
        n = len(c.calls)
        try:
            feed.refresh(clock())
        except FeedError:
            pass
        assert len(c.calls) - n <= 1                   # at most one request per refresh
        if len(c.calls) > n:
            probes.append(clock())
    gaps = [b - a for a, b in zip(probes, probes[1:])]
    assert gaps[:3] == [24, 48, 60] and set(gaps[3:]) == {60}   # 12 s, doubling, capped at 60 s
    assert feed.outage_since is not None
    c.fail = set()                                     # back up: the next probe clears the outage
    while feed.outage_since is not None:
        clock.advance(12)
        feed.refresh(clock())
    n = len(c.calls)
    clock.advance(60)
    feed.refresh(clock())
    assert len(c.calls) - n >= 3                       # normal polling again (depth + candles)
    assert feed.quote("AAAUSDT").ts == clock()


def test_public_feed_probe_timer_survives_a_backward_clock_step():
    clock = MockClock(T0)
    c, feed = _public(clock)
    feed.refresh(clock())
    c.fail = {"AAAUSDT", "BBBUSDT"}
    clock.advance(60)
    with pytest.raises(FeedError):
        feed.refresh(clock())
    clock.t -= 3600
    n = len(c.calls)
    feed.refresh(clock())                              # re-anchored: not due yet, nothing sent
    assert len(c.calls) == n
    clock.advance(12)
    with pytest.raises(FeedError):
        feed.refresh(clock())                          # the probe runs one interval later
    assert len(c.calls) == n + 1


def test_feed_params_validate_probe_cap():
    with pytest.raises(ValueError):
        FeedParams(outage_probe_max_s=5)
    with pytest.raises(ValueError):
        FeedParams(outage_probe_max_s=601)


@pytest.mark.parametrize("mode", ["mock", "shadow"])
def test_feed_loop_rides_out_an_outage(tmp_path, mode):
    clock = MockClock(1_700_000_000)
    c = FakeClient(clock)
    c.ranked = [_rules(SYM)]
    c.books = {SYM: ("100", "100.01", "1000")}
    sleeps, reps, during = [], [], []

    def sleep(s):
        sleeps.append(s)
        clock.advance(s)

    def on_tick(rep):
        reps.append(rep)
        if len(reps) == 3:
            c.fail = {SYM}
            during.append(len(c.calls))
        if len(reps) == 35:
            during.append(len(c.calls))
            c.fail = set()

    out = bot_main.run_feed_loop(mode, tmp_path / "f.db", cheap_config(), interval_s=12, max_ticks=40, client=c,
                                 clock=clock, sleep=sleep, on_tick=on_tick)
    assert out["ticks"] == 40 and set(sleeps) == {12} and len(sleeps) == 39        # cadence unchanged
    assert not out["kill_switch_engaged"] and out["consecutive_failures"] == 0
    assert out["feed_down_since"] is None and out["guardian"]["failed"] == 0
    assert any(r.feed_down_since is not None for r in reps) and not any(r.errors for r in reps)
    assert during[1] - during[0] <= 10                 # ~6.5 min of outage: a handful of probes only
    s = Store(tmp_path / "f.db")
    assert len(s.audit_events(actor="strategy", kind="heartbeat")) == 40
    assert len(s.audit_events(actor="bot", kind="feed_down")) == 1
    assert len(s.audit_events(actor="bot", kind="feed_restored")) == 1
    assert s.latest_killswitch() is None
    s.close()


# ---------------------------------------------------------------------------- healthcheck
NOW = 1_800_000_000.0


def _status(db, **kw):
    ns = hc.argparse.Namespace(db=str(db), max_heartbeat_age_s=120.0, alert_window_min=15.0,
                               allow_killswitch=False, recorder_dir=None, max_recorder_age_s=300.0,
                               max_feed_outage_s=900.0)
    for k, v in kw.items():
        setattr(ns, k, v)
    return hc.run(ns, now=NOW)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "bot.db")
    s.append_audit_event("bot", "startup", {"run": {"mode": "shadow", "profile": "strict",
                                                    "guardian": {"enabled": False}}}, ts=NOW - 5000)
    s.heartbeat("strategy", ts=NOW - 5)
    yield s
    s.close()


def test_healthcheck_short_outage_warns_and_stays_ok(store):
    store.append_audit_event("bot", "feed_down", {"since": NOW - 300, "error": "FeedError"}, ts=NOW - 300)
    code, st = _status(store.path)
    assert code == 0 and st["ok"] and st["feed_down_since"] == NOW - 300 and st["feed_outage_s"] == 300
    assert any("market data down for 300s" in w for w in st["warnings"]) and not st["problems"]


def test_healthcheck_long_outage_fails_and_the_limit_is_configurable(store):
    store.append_audit_event("bot", "feed_down", {"since": NOW - 1000, "error": "FeedError"}, ts=NOW - 1000)
    code, st = _status(store.path)
    assert code == 1 and any("market data down for 1000s" in p for p in st["problems"])
    assert _status(store.path, max_feed_outage_s=1200.0)[0] == 0
    store.append_audit_event("bot", "feed_restored", {"since": NOW - 1000, "outage_s": 990}, ts=NOW - 10)
    code, st = _status(store.path)
    assert code == 0 and st["feed_down_since"] is None and not st["warnings"]


def test_healthcheck_uses_the_bots_own_duration_after_a_clock_step(store):
    store.append_audit_event("bot", "feed_down", {"since": NOW + 100, "error": "FeedError"}, ts=NOW + 100)
    store.append_error("bot.feed_down", "market data down for 1200s", {"since": NOW + 100, "outage_s": 1200.0},
                       ts=NOW)
    code, st = _status(store.path)
    assert code == 1 and st["feed_outage_s"] == 1200


def test_healthcheck_ignores_an_outage_of_a_previous_run(store):
    store.append_audit_event("bot", "feed_down", {"since": NOW - 4000}, ts=NOW - 4000)
    store.append_audit_event("bot", "startup", {"run": {"mode": "shadow", "guardian": {"enabled": False}}},
                             ts=NOW - 60)
    code, st = _status(store.path)
    assert code == 0 and st["feed_down_since"] is None


def test_healthcheck_cli_flag(store, capsys, monkeypatch):
    store.append_audit_event("bot", "feed_down", {"since": NOW - 1000}, ts=NOW - 1000)
    monkeypatch.setattr(hc.time, "time", lambda: NOW)
    assert hc.main(["--db", str(store.path)]) == 1
    assert hc.main(["--db", str(store.path), "--max-feed-outage-s", "2000"]) == 0
    st = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert st["ok"] and st["feed_outage_s"] == 1000


def test_healthcheck_on_a_real_outage_run(tmp_path):
    rig, feed = manual_rig(tmp_path)
    feed.refresh_error = DOWN
    run(rig, 50, step=12.0)                            # 10 minutes
    ns = dict(max_heartbeat_age_s=120.0, alert_window_min=15.0, allow_killswitch=False, recorder_dir=None,
              max_recorder_age_s=300.0, max_feed_outage_s=900.0)
    code, st = hc.run(hc.argparse.Namespace(db=str(rig.store.path), **ns), now=rig.clock())
    assert code == 0 and st["feed_outage_s"] >= 588 and st["warnings"] and not st["killswitch_engaged"]
    run(rig, 30, step=12.0)                            # 16 minutes
    code, st = hc.run(hc.argparse.Namespace(db=str(rig.store.path), **ns), now=rig.clock())
    assert code == 1 and st["feed_outage_s"] > 900 and not st["killswitch_engaged"]
    rig.close()
