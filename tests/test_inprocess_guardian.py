"""The guardian of a mock/shadow bot runs IN the bot process, on the bot's broker object.

Regression: the standalone guardian CLI built its own in-memory 10000 USDT mock broker and wrote
equity start-of-day / high-water-mark rows into the shared DB. On a bot started with
--balance 1000 that looked like a ~90 % drawdown, and its balance check compared the bot's ledger
with the wrong broker (false balance-mismatch kill switches)."""
from __future__ import annotations

import dataclasses
from decimal import Decimal as D

import pytest
from test_bot_feed import FakeClient, _rules
from test_bot_helpers import SYM, WINNER, Rig, cheap_config, series

from wallexbot import cli
from wallexbot.bot import main as bot_main
from wallexbot.bot.feed import explode_bars
from wallexbot.data.candles import write_candles_csv
from wallexbot.exchange.mock_wallex import MockClock
from wallexbot.guardian.main import InProcessGuardian, demo_db_problem
from wallexbot.guardian.main import main as guardian_main
from wallexbot.store import Store
from wallexbot.types import Side


def _replay_dir(tmp_path, cont=WINNER):
    data = tmp_path / "history"
    write_candles_csv(data / SYM / f"{SYM}_1m_2023-11.csv", explode_bars(series(cont)))
    return data


# ---------------------------------------------------------------------------- standalone refusal
def _bot_db(tmp_path):
    db = tmp_path / "bot.db"
    bot_main.run_replay(db, _replay_dir(tmp_path), cheap_config(), max_ticks=3, guardian=False)
    return db


def _counts(db):
    s = Store(db)
    try:
        return {t: len(s.rows(t)) for t in ("equity_sod", "equity_hwm", "audit_events", "killswitch_events")}
    finally:
        s.close()


@pytest.mark.parametrize("extra", [[], ["--demo-db"]])
def test_standalone_guardian_refuses_a_bot_database(tmp_path, capsys, extra):
    db = _bot_db(tmp_path)
    before = _counts(db)
    assert cli.main(["guardian", "--db", str(db), "--mode", "shadow", *extra, "--ticks", "1", "--interval", "0"]) == 2
    err = capsys.readouterr().err
    assert "refused" in err and "DEMO" in err and "in-process" in err
    assert ("bot audit event" in err) == bool(extra)
    assert _counts(db) == before                    # nothing written: no equity rows, no kill switch


def test_standalone_guardian_needs_demo_flag_even_on_a_new_db(tmp_path, capsys):
    db = tmp_path / "new.db"
    assert guardian_main(["--db", str(db), "--mode", "mock", "--ticks", "1", "--interval", "0"]) == 2
    assert "--demo-db" in capsys.readouterr().err and not db.exists()


def test_standalone_guardian_refuses_foreign_equity_rows(tmp_path, capsys):
    db = tmp_path / "x.db"
    s = Store(db)
    s.append_equity_hwm(D("1000"))
    s.close()
    assert "not written by the demo guardian" in demo_db_problem(db)
    assert guardian_main(["--db", str(db), "--mode", "mock", "--demo-db", "--ticks", "1", "--interval", "0"]) == 2
    assert _counts(db)["equity_hwm"] == 1


def test_standalone_demo_runs_on_new_db_and_reruns_on_its_own(tmp_path):
    db = tmp_path / "demo.db"
    args = ["--db", str(db), "--mode", "mock", "--demo-db", "--ticks", "1", "--interval", "0"]
    assert demo_db_problem(db) is None
    assert guardian_main(args) == 0
    assert demo_db_problem(db) is None              # its own demo marker: the demo may run again
    assert guardian_main(args) == 0


# ---------------------------------------------------------------------------- in-process
def test_replay_runs_the_guardian_in_process_on_the_bot_broker(tmp_path):
    db = tmp_path / "r.db"
    out = bot_main.run_replay(db, _replay_dir(tmp_path), cheap_config(), balances={"USDT": D("1000")})
    assert out["guardian"]["ticks"] == out["ticks"] and out["guardian"]["failed"] == 0
    assert out["intents_by_state"] == {"filled": 3}           # the guardian did not disturb the trade
    s = Store(db)
    try:
        hwm, sods = s.equity_hwm(), [D(r["equity"]) for r in s.rows("equity_sod")]
        assert D("990") < min(sods) and hwm < D("1100")        # the bot's 1000 USDT, not a 10000 demo broker
        assert not s.rows("killswitch_events")                 # no false drawdown / mismatch kill switch
        assert not [e for e in s.rows("errors") if e["source"].startswith("guardian.")]
        assert len(s.audit_events("guardian", "heartbeat")) == out["ticks"]
        run = s.audit_events("bot", "startup")[0]["payload"]["run"]
        assert run["guardian"]["enabled"] is True and run["mode"] == "mock"
    finally:
        s.close()


def test_no_guardian_flag(tmp_path, capsys):
    db = tmp_path / "r.db"
    assert bot_main.main(["--db", str(db), "--mode", "mock", "--replay-from", str(_replay_dir(tmp_path)),
                          "--max-ticks", "3", "--no-guardian"]) == 0
    assert "guardian: DISABLED" in capsys.readouterr().out
    s = Store(db)
    try:
        assert s.audit_events("guardian") == []
        assert s.audit_events("bot", "startup")[0]["payload"]["run"]["guardian"]["enabled"] is False
    finally:
        s.close()


def test_guardian_needs_a_database_file(tmp_path, capsys):
    assert bot_main.main(["--db", ":memory:", "--mode", "mock", "--replay-from", str(tmp_path)]) == 2
    assert "needs a database file" in capsys.readouterr().err


def test_feed_loop_ticks_guardian_between_long_sleeps(tmp_path):
    """Bot interval 60 s, guardian interval 15 s: one guardian tick after each bot tick plus three
    while the bot sleeps; same broker object for both."""
    clock = MockClock(1_700_000_000)
    c = FakeClient(clock)
    c.ranked = [_rules(SYM)]
    c.books = {SYM: ("100", "100.01", "1000")}

    def sleep(s):
        clock.advance(s)
    out = bot_main.run_feed_loop("mock", tmp_path / "f.db", cheap_config(), interval_s=60, max_ticks=3, client=c,
                                 clock=clock, sleep=sleep, guardian_interval_s=15)
    assert out["ticks"] == 3 and out["guardian"] == {"ticks": 3 + 2 * 3, "failed": 0}
    s = Store(tmp_path / "f.db")
    try:
        assert s.equity_hwm() == D("1000")                       # the paper broker's balance
    finally:
        s.close()


def test_inprocess_guardian_flattens_through_the_bot_broker_on_hard_breach(tmp_path):
    cfg = cheap_config()
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, daily_loss_soft_pct=D("0.05"),
                                                            daily_loss_hard_pct=D("0.1")))
    rig = Rig(tmp_path, config=cfg)
    gd = InProcessGuardian(rig.store.path, rig.broker, rig.risk.rules, cfg, clock=rig.clock,
                           sleep=rig.clock.advance, universe=lambda: [SYM])
    while not rig.bot.positions:
        rig.bot.tick()
        gd.tick()
        rig.clock.advance(60)
    q = rig.broker.quote(SYM)
    rig.broker.set_price(SYM, q.bid * D("0.97"), q.ask * D("0.97"))   # -3 %: hard daily-loss breach
    rig.store.heartbeat("strategy", ts=rig.clock())
    rep = gd.tick()
    assert rep is not None and "flatten:hard" in rep.actions and rig.ks.is_engaged()
    sells = [o for o in rig.broker.orders.values() if o.side is Side.SELL]
    assert sells and all(o.client_id in {r.intent.client_id for r in rig.intents(Side.SELL)} for o in sells)
    assert rig.store.latest_positions()[SYM].quantity < D("0.001")
    gd.close()
    rig.close()


def test_inprocess_guardian_tick_never_raises(tmp_path):
    rig = Rig(tmp_path)

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("broker down")
    gd = InProcessGuardian(rig.store.path, Broken(), rig.risk.rules, rig.cfg, clock=rig.clock,
                           sleep=rig.clock.advance, max_consecutive_failures=2)
    assert gd.tick() is None and gd.tick() is None
    assert gd.failed_ticks == 2 and rig.ks.is_engaged()            # fail closed: entries off
    gd.close()
    rig.close()
