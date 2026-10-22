"""deploy/smoke_shadow_fill.py, offline: the same script logic over ReplayFeed (no network, simulated clock)."""
from __future__ import annotations

import importlib.util
import sys
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest
from test_bot_helpers import RULES, STEP, SYM, T0, WINNER, cheap_config, series

from wallexbot.bot.feed import ReplayFeed, explode_bars
from wallexbot.bot.loop import simple_atr
from wallexbot.exchange.mock_wallex import MockClock
from wallexbot.exchange.shadow import ShadowBroker
from wallexbot.store import Store
from wallexbot.strategy.contract import MarketContext, PositionState
from wallexbot.strategy.exits import ExitParams, make_policy
from wallexbot.types import IntentState, Side

D = Decimal
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "smoke_shadow_fill.py"
MC = MarketContext(D("4"), D("1"), D("0.001"))


def _load():
    spec = importlib.util.spec_from_file_location("deploy_smoke_shadow_fill", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


smoke = _load()


def _rig(bars=None):
    cfg = cheap_config()
    clock = MockClock(T0 + 121 * STEP)
    feed = ReplayFeed({SYM: explode_bars(bars if bars is not None else series(WINNER))}, RULES, cfg)
    return cfg, clock, feed


def _run(tmp_path, **kw):
    cfg, clock, feed = _rig()
    lines: list[str] = []
    res = smoke.run_smoke(tmp_path / "scratch", cfg, feed, RULES, clock=clock, sleep=clock.advance,
                          out=lines.append, **kw)
    return res, lines


def test_full_round_trip_passes_every_invariant(tmp_path):
    res, lines = _run(tmp_path, hold_s=30.0)
    failed = [c for c in res.checks if not c.ok]
    assert res.ok and not failed, failed
    assert res.stopped_because == "round_trip_complete" and res.symbol == SYM
    assert lines[0] == f"*** {smoke.BANNER} ***"
    assert any("SMOKE RESULT: PASS" in x for x in lines)
    names = {c.name for c in res.checks}
    assert {"healthcheck ok", "kill switch never engaged", "guardian in-process ran with 0 failures"} <= names
    assert res.summary["fills"] >= 2 and res.summary["intents_by_state"] == {"filled": 2}


def test_audit_db_has_one_forced_round_trip_labelled_smoke_forced(tmp_path):
    _run(tmp_path, hold_s=30.0)
    store = Store(tmp_path / "scratch" / "smoke.db")
    try:
        events = store.audit_events(actor="bot")
        assert events and all(e["payload"]["profile"] == "smoke_forced" for e in events)
        assert store.audit_events(actor="bot", kind="startup")[-1]["payload"]["run"]["engine"] == "smoke_forced"
        sigs = store.rows("signals")
        assert len(sigs) == 1 and sigs[0]["symbol"] == SYM
        intents = store.intents_in_states(frozenset(IntentState))
        assert sorted(r.intent.side.value for r in intents) == ["BUY", "SELL"]
        assert all(r.state is IntentState.FILLED for r in intents)
        assert {r.intent.side for r in intents} == {Side.BUY, Side.SELL}
        pos = store.latest_positions()[SYM]
        assert pos.quantity < D("0.0001") and pos.realized_pnl != 0       # at most fee dust left; fees make P&L non-zero
        exits = store.audit_events(actor="bot", kind="exit_requested")
        assert [e["payload"]["reason"] for e in exits] == ["smoke_forced_exit"]   # via the bot's own request path
        assert [e["payload"]["ok"] for e in store.audit_events(actor="smoke", kind="result")] == [True]
    finally:
        store.close()
    assert (tmp_path / "scratch" / "shadow_journal.jsonl").exists()


def test_forced_signal_engine_follows_the_signal_engine_contract():
    cfg, clock, feed = _rig()
    feed.refresh(clock())
    bars = feed.closed_bars(SYM, 60)
    eng = smoke.ForcedSignalEngine(SYM, RULES[SYM])
    assert eng.evaluate("OTHERUSDT", bars, MC) is None               # only the chosen symbol
    sig = eng.evaluate(SYM, bars, MC)
    assert sig is not None and sig.symbol == SYM and sig.bar_ts == bars[-1].ts
    atr = simple_atr(bars, 14)
    assert sig.atr == atr and sig.entry_ref_price == bars[-1].close
    assert sig.stop_price == (bars[-1].close - D("1.5") * atr).quantize(D("0.01"), rounding=ROUND_DOWN)
    assert 0 < sig.stop_price < sig.entry_ref_price and sig.signal_id.startswith("sig_")
    assert sig.reasons["forced"] == "true"
    assert eng.evaluate(SYM, bars, MC) is None                       # exactly one signal, ever


def test_forced_signal_engine_uses_the_live_reference_price_when_given():
    cfg, clock, feed = _rig()
    feed.refresh(clock())
    bars = feed.closed_bars(SYM, 60)
    sig = smoke.ForcedSignalEngine(SYM, RULES[SYM], lambda s: D("123.45")).evaluate(SYM, bars, MC)
    assert sig.entry_ref_price == D("123.45") and sig.stop_price < D("123.45")


def test_engine_without_enough_bars_raises_instead_of_signalling():
    cfg, clock, feed = _rig()
    feed.refresh(clock())
    with pytest.raises(ValueError):
        smoke.ForcedSignalEngine(SYM, RULES[SYM]).evaluate(SYM, feed.closed_bars(SYM, 5), MC)


def test_forced_exit_policy_delegates_and_adds_one_level_after_the_hold():
    clock = MockClock(1000.0)
    inner = make_policy("E4", ExitParams())
    pol = smoke.ForcedExitPolicy(inner, clock, hold_s=40.0)
    st = PositionState(SYM, D("100"), 0, D("1"), D("98"), D("99"), D("100"), 0)
    base = tuple(inner.targets(st))
    assert base and pol.targets(st) == base                          # before the hold: E4 only
    clock.advance(39.0)
    assert pol.targets(st) == base
    clock.advance(2.0)
    t = pol.targets(st)
    assert t[0].reason == "smoke_forced_exit" and t[0].fraction >= 1 and t[1:] == base


def test_refuses_any_mode_but_shadow(tmp_path, capsys):
    assert smoke.main([str(tmp_path / "a"), "--mode", "live"]) == 2
    assert smoke.main([str(tmp_path / "b"), "--mode", "mock"]) == 2
    assert "shadow mode only" in capsys.readouterr().err
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_refuses_to_run_next_to_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WALLEX_API_KEY_TRADE", "x")
    assert smoke.main([str(tmp_path / "c")]) == 2
    assert "credential-like" in capsys.readouterr().err
    assert smoke._secret_env({"PATH": "/bin", "WALLEX_ARM_LIVE": "1"}) == ["WALLEX_ARM_LIVE"]
    assert smoke._secret_env({"PATH": "/bin", "WALLEX_ARM_LIVE": ""}) == []


def test_refuses_a_non_empty_scratch_dir_and_a_bad_interval(tmp_path):
    cfg, clock, feed = _rig()
    work = tmp_path / "s"
    work.mkdir()
    (work / "smoke.db").write_bytes(b"")
    with pytest.raises(smoke.SmokeRefused, match="empty scratch"):
        smoke.run_smoke(work, cfg, feed, RULES, clock=clock, sleep=clock.advance)
    with pytest.raises(smoke.SmokeRefused, match="interval"):
        smoke.run_smoke(tmp_path / "t", cfg, feed, RULES, clock=clock, sleep=clock.advance, interval_s=60.0)


def test_symbol_choice_prefers_btc_then_the_most_liquid_survivor():
    with pytest.raises(smoke.SmokeRefused):
        smoke.pick_symbol([])
    assert smoke.pick_symbol(["ETHUSDT", "BTCUSDT"]) == "BTCUSDT"
    assert smoke.pick_symbol(["ETHUSDT", "XRPUSDT"]) == "ETHUSDT"    # BTC failed the filter: next most liquid


def test_a_timeout_fails_the_run_instead_of_passing(tmp_path):
    res, lines = _run(tmp_path, hold_s=10_000.0, max_wall_s=120.0)
    assert not res.ok and res.stopped_because == "timeout"
    assert any("SMOKE RESULT: FAIL" in x for x in lines)


def test_the_broker_it_asserts_is_the_shadow_broker():
    assert smoke.ShadowBroker is ShadowBroker


def test_script_lives_outside_src_and_never_sends_orders():
    text = SCRIPT.read_text()
    assert SCRIPT.parent == ROOT / "deploy"
    for forbidden in ("submit_order", ".submit(", "place_order", "create_order", "wallex_private", "mock_wallex"):
        assert forbidden not in text, forbidden
