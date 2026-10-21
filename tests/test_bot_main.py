"""bot.main: mode gating, broker factory, offline replay run, PublicFeed wiring (fake client)."""
import re
import sys
from decimal import Decimal

import pytest
from test_bot_feed import FakeClient, _rules
from test_bot_helpers import SYM, WINNER, cheap_config, series

from wallexbot.bot import main as bot_main
from wallexbot.bot.feed import explode_bars
from wallexbot.config import DEFAULT_PATH
from wallexbot.data.candles import write_candles_csv
from wallexbot.exchange.mock_wallex import MockClock
from wallexbot.store import Store
from wallexbot.types import IntentState

D = Decimal


def test_live_is_refused(tmp_path, capsys):
    db = tmp_path / "x.db"
    assert bot_main.main(["--db", str(db), "--mode", "live"]) == 2
    err = capsys.readouterr().err
    assert "refused" in err and "live trading is not wired" in err
    assert not db.exists()


@pytest.mark.parametrize("argv", [["--mode", "paper"], ["--mode", "shadow", "--replay-from", "x"],
                                  ["--mode", "mock", "--config", "/nonexistent.yaml"]])
def test_other_bad_invocations_are_refused(tmp_path, capsys, argv):
    assert bot_main.main(["--db", str(tmp_path / "x.db"), *argv]) == 2
    assert "refused" in capsys.readouterr().err


def test_make_broker_modes(monkeypatch):
    cfg = cheap_config()
    rules = {SYM: _rules(SYM)}
    with pytest.raises(bot_main.BotSetupError, match="not wired"):
        bot_main.make_broker("live", config=cfg, clock=MockClock(), rules=rules)
    paper = bot_main.make_broker("mock", config=cfg, clock=MockClock(), rules=rules)
    assert paper.balances()["USDT"].free == D("1000")
    monkeypatch.setitem(sys.modules, "wallexbot.exchange.shadow", None)        # not installed
    with pytest.raises(bot_main.BotSetupError, match="ShadowBroker"):
        bot_main.make_broker("shadow", config=cfg, clock=MockClock(), rules=rules, book_source=lambda s: None)
    monkeypatch.undo()                                                         # the REAL ShadowBroker
    from wallexbot.exchange.shadow import ShadowBroker
    b = bot_main.make_broker("shadow", config=cfg, clock=MockClock(), rules=rules, book_source=len,
                             balances={"USDT": D("250")}, journal_path=None)
    assert isinstance(b, ShadowBroker) and b.balances()["USDT"].free == D("250")
    assert b.maker_rate == cfg.maker_rate and b.taker_rate == cfg.taker_rate


def _cheap_yaml(tmp_path):
    text = DEFAULT_PATH.read_text()
    text = re.sub(r'(taker_rate|maker_rate): "[0-9.]+"', r'\1: "0.001"', text)
    p = tmp_path / "cfg.yaml"
    p.write_text(text)
    return p


def test_offline_replay_cli_end_to_end(tmp_path, capsys):
    data = tmp_path / "history"
    m1 = explode_bars(series(WINNER))
    write_candles_csv(data / SYM / f"{SYM}_1m_2023-11.csv", m1)
    db = tmp_path / "replay.db"
    rc = bot_main.main(["--db", str(db), "--mode", "mock", "--replay-from", str(data),
                        "--config", str(_cheap_yaml(tmp_path)), "--interval-s", "60"])
    out = capsys.readouterr().out
    assert rc == 0 and "guardian.main" in out and "bot stopped" in out
    s = Store(db)
    intents = s.intents_in_states(set(IntentState))
    assert len(intents) == 3 and all(r.state.value == "filled" for r in intents)
    assert s.latest_positions()[SYM].realized_pnl > 0
    assert len(s.audit_events(actor="strategy", kind="heartbeat")) > 100
    s.close()


def test_replay_with_default_fees_and_max_ticks(tmp_path):
    data = tmp_path / "history"
    write_candles_csv(data / SYM / f"{SYM}_1m_2023-11.csv", explode_bars(series(WINNER)))
    out = bot_main.run_replay(tmp_path / "r.db", data, cheap_config(), max_ticks=5)
    assert out["ticks"] == 5 and out["intents"] == 0
    with pytest.raises(bot_main.BotSetupError):
        bot_main.run_replay(tmp_path / "r2.db", tmp_path / "empty", cheap_config())


def test_feed_loop_mock_mode_with_fake_public_client(tmp_path):
    clock = MockClock(1_700_000_000)
    c = FakeClient(clock)
    c.ranked = [_rules(SYM)]
    c.books = {SYM: ("100", "100.01", "1000")}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.advance(s)

    out = bot_main.run_feed_loop("mock", tmp_path / "f.db", cheap_config(), interval_s=12, max_ticks=4, client=c,
                                 clock=clock, sleep=sleep)
    assert out["ticks"] == 4 and sleeps == [12, 12, 12] and out["consecutive_failures"] == 0
    s = Store(tmp_path / "f.db")
    assert len(s.audit_events(actor="strategy", kind="heartbeat")) == 4
    s.close()
