"""`python -m wallexbot bot` and the optional shadow advisor wiring. Offline: replayed candles, fake transport."""
import json
import re
import subprocess
import sys

import pytest
from test_ai_advisor import KEY, FakeTransport
from test_bot_helpers import SYM, WINNER, series

from wallexbot import advisor_bridge, cli
from wallexbot.ai.advisor import LoggedAdvisor, NullAdvisor
from wallexbot.bot.feed import explode_bars
from wallexbot.config import DEFAULT_PATH
from wallexbot.data.candles import write_candles_csv
from wallexbot.store import Store
from wallexbot.types import IntentState


def _setup(tmp_path):
    data = tmp_path / "history"
    write_candles_csv(data / SYM / f"{SYM}_1m_2023-11.csv", explode_bars(series(WINNER)))
    text = re.sub(r'(taker_rate|maker_rate): "[0-9.]+"', r'\1: "0.001"', DEFAULT_PATH.read_text())
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(text)
    return data, cfg, tmp_path / "bot.db"


def _argv(data, cfg, db, *extra):
    return ["bot", "--db", str(db), "--mode", "mock", "--replay-from", str(data), "--config", str(cfg),
            "--interval-s", "60", *extra]


def test_help_lists_every_command():
    out = subprocess.run([sys.executable, "-m", "wallexbot", "--help"], capture_output=True, text=True, check=True).stdout
    for name in ("demo", "backtest", "doctor", "guardian", "bot"):
        assert re.search(rf"^\s+{name}\s", out, re.M), name


def test_bot_subcommand_replay_smoke(tmp_path, capsys):
    data, cfg, db = _setup(tmp_path)
    assert cli.main(_argv(data, cfg, db)) == 0
    assert "bot stopped" in capsys.readouterr().out
    s = Store(db)
    assert len(s.rows("fills")) >= 2 and s.audit_events(actor="advisor") == []
    s.close()


@pytest.mark.parametrize("argv", [["bot", "--db", "x.db", "--mode", "live"], ["bot", "--db", "x.db", "--mode=live"],
                                  ["bot", "--arm-live", "--db", "x.db", "--mode", "mock"]])
def test_live_is_refused_with_the_arm_live_message(argv, capsys):
    assert cli.main(argv) == 2
    assert cli.LIVE_REFUSAL in capsys.readouterr().err


def test_bot_other_modes_and_missing_options_are_refused_by_the_bot_parser(tmp_path, capsys):
    assert cli.main(["bot", "--db", str(tmp_path / "x.db"), "--mode", "paper"]) == 2
    assert "refused" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        cli.main(["bot", "--mode", "mock"])
    assert e.value.code == 2


def test_advisor_shadow_without_key_warns_and_uses_null_advisor(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data, cfg, db = _setup(tmp_path)
    assert cli.main(_argv(data, cfg, db, "--advisor", "shadow")) == 0
    cap = capsys.readouterr()
    assert "ANTHROPIC_API_KEY is not set" in cap.err and "NullAdvisor" in cap.err and "bot stopped" in cap.out
    bridge, rec = advisor_bridge.build_advisor("shadow", db, env={})
    assert isinstance(bridge.advisor, NullAdvisor)


def test_advisor_off_never_reads_the_key(monkeypatch):
    monkeypatch.setattr(advisor_bridge, "load_secret", lambda *a, **k: pytest.fail("key read while off"))
    assert advisor_bridge.build_advisor("off", ":memory:") == (None, None)


def test_advisor_shadow_records_verdicts_in_the_store_and_never_changes_trading(tmp_path, monkeypatch, capsys):
    data, cfg, db = _setup(tmp_path)
    base_db = tmp_path / "base.db"
    assert cli.main(_argv(data, cfg, base_db)) == 0
    capsys.readouterr()
    transport = FakeTransport()                      # replies allow @ 0.5; shadow mode must ignore it
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    real = advisor_bridge.build_advisor
    monkeypatch.setattr(advisor_bridge, "build_advisor", lambda *a, **k: real(*a, transport=transport, **k))
    assert cli.main(_argv(data, cfg, db, "--advisor", "shadow")) == 0
    assert KEY not in capsys.readouterr().out
    assert len(transport.calls) >= 1
    s, b = Store(db), Store(base_db)
    recs = s.audit_events(actor="advisor", kind="verdict")
    assert recs and all(KEY not in json.dumps(r) for r in recs)
    payload = recs[0]["payload"]
    assert payload["mode"] == "shadow" and payload["returned_decision"] == "abstain"
    assert payload["verdict"]["decision"] == "allow"
    # shadow: the same orders as the run without an advisor
    def orders(st):
        return [(r.intent.side, str(r.intent.quantity)) for r in st.intents_in_states(frozenset(IntentState))]
    assert orders(s) == orders(b) and orders(s)
    s.close()
    b.close()


def test_make_advisor_wiring_is_shadow_only(tmp_path):
    bridge, rec = advisor_bridge.build_advisor("shadow", tmp_path / "a.db", env={"ANTHROPIC_API_KEY": KEY},
                                               transport=FakeTransport())
    assert isinstance(bridge.advisor, LoggedAdvisor) and bridge.advisor.apply is False
