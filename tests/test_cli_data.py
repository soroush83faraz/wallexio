"""`python -m wallexbot record` / `history`: public-data CLIs, tested with fake clients (no network)."""
from __future__ import annotations

import json
import os
import signal
from decimal import Decimal
from types import SimpleNamespace

import pytest

from wallexbot import cli
from wallexbot.data import collect
from wallexbot.data.candles import Candle
from wallexbot.data.history import load_history
from wallexbot.exchange.wallex_public import WallexHTTPError
from wallexbot.types import SymbolRules

NOW = 1_700_000_000 // 60 * 60 + 30


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


class FakePublic:
    """Stands in for WallexPublicClient: only the four public read methods exist."""

    def __init__(self, on_depth=None):
        self.calls = []
        self.on_depth = on_depth

    def rank_universe(self, quote, top_n):
        self.calls.append(("rank", quote, top_n))
        return [SymbolRules(s, s[:-4], "USDT", 4, 2) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")][:top_n]

    def depth(self, symbol):
        self.calls.append(("depth", symbol))
        if self.on_depth:
            self.on_depth()
        return SimpleNamespace(ts=1_700_000_000.0, bids=[(Decimal("99"), Decimal("1"))],
                               asks=[(Decimal("101"), Decimal("1"))])

    def trades(self, symbol):
        self.calls.append(("trades", symbol))
        return [SimpleNamespace(ts=1_700_000_000.0, price=Decimal("100"), quantity=Decimal("1"), is_buy=True)]

    def candles(self, symbol, resolution, start, end):
        self.calls.append(("candles", symbol, start, end))
        return [Candle(t, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), Decimal("1"))
                for t in range(start, end + 1, 60)]


# ---------------------------------------------------------------- wiring
def test_help_lists_the_commands(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "record" in out and "history" in out


def test_subcommand_options(capsys):
    a = _args("record", "--out", "x", "--symbols", "BTCUSDT,ETHUSDT", "--interval-s", "20", "--max-cycles", "3")
    assert (str(a.out), a.symbols, a.interval_s, a.max_cycles, a.top_n) == ("x", ["BTCUSDT,ETHUSDT"], 20.0, 3, None)
    h = _args("history", "--out", "y", "--top-n", "5", "--days", "7", "--max-requests", "9")
    assert (str(h.out), h.top_n, h.days, h.max_requests) == ("y", 5, 7, 9)
    d = _args("history")
    assert d.days == 45 and d.max_requests == 1500


def test_arm_live_is_still_refused_for_the_new_commands(capsys):
    assert cli.main(["record", "--arm-live"]) == 2
    assert cli.main(["history", "--arm-live"]) == 2


def test_default_client_is_the_host_pinned_public_client():
    from wallexbot.exchange.wallex_public import WallexPublicClient
    c = collect._public_client(None)
    assert isinstance(c, WallexPublicClient)
    with pytest.raises(Exception, match="non-pinned"):
        c._check_url("https://evil.example/v1/depth")


def test_collect_imports_no_order_path_modules():
    import ast
    tree = ast.parse(collect.__loader__.get_source(collect.__name__))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in mods if m and set(m.split(".")) & {"broker", "oms", "wallex_private", "risk", "guardian", "bot"}}
    assert {m for m in mods if m and m.startswith("wallexbot")} <= {
        "wallexbot.config", "wallexbot.data.recorder", "wallexbot.data.history", "wallexbot.exchange.wallex_public"}


# ---------------------------------------------------------------- symbols
def test_parse_and_resolve_symbols():
    assert collect.parse_symbols(["BTCUSDT,ETHUSDT", "ETHUSDT", "SOLUSDT"]) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    with pytest.raises(collect.UsageError):
        collect.parse_symbols(["btc/usdt; rm"])
    fake = FakePublic()
    assert collect.resolve_symbols(fake, None, 2) == ["BTCUSDT", "ETHUSDT"]
    assert fake.calls == [("rank", "USDT", 2)]
    assert collect.resolve_symbols(fake, None, None) == list(collect.DEFAULT_SYMBOLS)
    with pytest.raises(collect.UsageError):
        collect.resolve_symbols(fake, ["BTCUSDT"], 2)
    with pytest.raises(collect.UsageError):
        collect.resolve_symbols(fake, None, 0)


# ---------------------------------------------------------------- record
def test_record_one_cycle(tmp_path, capsys):
    out = tmp_path / "rec"
    fake = FakePublic()
    rc = collect.run_record(_args("record", "--out", str(out), "--symbols", "BTCUSDT,ETHUSDT", "--max-cycles", "1"),
                            client=fake)
    assert rc == 0
    files = sorted(p.name for p in out.glob("*.jsonl"))
    assert len(files) == 2 and files[0].startswith("depth_") and files[1].startswith("trades_")
    rows = [json.loads(ln) for ln in next(out.glob("depth_*.jsonl")).read_text().splitlines()]
    assert {r["symbol"] for r in rows} == {"BTCUSDT", "ETHUSDT"}
    assert {c[0] for c in fake.calls} == {"depth", "trades"}                     # only public reads
    assert "recorder stopped" in capsys.readouterr().out


def test_record_top_n_resolves_universe(tmp_path):
    fake = FakePublic()
    assert collect.run_record(_args("record", "--out", str(tmp_path), "--top-n", "2", "--max-cycles", "1"),
                              client=fake) == 0
    assert fake.calls[0] == ("rank", "USDT", 2)
    assert {c[1] for c in fake.calls if c[0] == "depth"} == {"BTCUSDT", "ETHUSDT"}


def test_record_refuses_bad_input(tmp_path, capsys):
    fake = FakePublic()
    for argv in (["--symbols", "bad symbol!"], ["--symbols", "BTCUSDT", "--top-n", "3"],
                 ["--interval-s", "0.1"], ["--max-cycles", "0"]):
        assert collect.run_record(_args("record", "--out", str(tmp_path), *argv), client=fake) == 2
    assert fake.calls == [] and not list(tmp_path.iterdir())
    assert "refused" in capsys.readouterr().err


def test_record_sigterm_finishes_the_cycle_and_exits_zero(tmp_path, capsys):
    before = signal.getsignal(signal.SIGTERM)
    fake = FakePublic(on_depth=lambda: os.kill(os.getpid(), signal.SIGTERM))   # arrives mid-cycle
    rc = collect.run_record(_args("record", "--out", str(tmp_path), "--symbols", "BTCUSDT,ETHUSDT",
                                  "--interval-s", "60"), client=fake)         # no --max-cycles: only the signal stops it
    assert rc == 0
    assert sum(1 for c in fake.calls if c[0] == "depth") == 2                  # the whole cycle completed
    assert "recorder stopped" in capsys.readouterr().out
    assert signal.getsignal(signal.SIGTERM) is before                          # handlers restored


# ---------------------------------------------------------------- history
def _history(tmp_path, *argv, client, **kw):
    return collect.run_history(_args("history", "--out", str(tmp_path), *argv), client=client,
                               clock=lambda: NOW, sleep=lambda s: None, **kw)


def test_history_downloads_and_resumes(tmp_path, capsys):
    fake = FakePublic()
    assert _history(tmp_path, "--symbols", "BTCUSDT", "--days", "1", client=fake) == 0
    n_first = len(fake.calls)
    assert n_first >= 3 and all(c[0] == "candles" for c in fake.calls)         # 1440 bars -> 3 chunks of <=600
    last_closed = NOW // 60 * 60 - 60
    got = load_history(tmp_path, "BTCUSDT", last_closed - 86_400, last_closed)
    assert len(got) >= 1430 and got[-1].ts == last_closed
    assert "history done" in capsys.readouterr().out
    fake2 = FakePublic()
    assert _history(tmp_path, "--symbols", "BTCUSDT", "--days", "1", client=fake2) == 0
    assert fake2.calls == []                                                   # resumable: nothing left to fetch


def test_history_budget_exhaustion_is_incomplete_but_saved(tmp_path, capsys):
    fake = FakePublic()
    rc = _history(tmp_path, "--symbols", "BTCUSDT", "ETHUSDT", "--days", "2", "--max-requests", "2", client=fake)
    assert rc == 1 and len([c for c in fake.calls if c[0] == "candles"]) == 2
    assert "re-run to resume" in capsys.readouterr().out
    assert load_history(tmp_path, "BTCUSDT", 0, NOW)                             # partial progress is on disk


def test_history_top_n_and_validation(tmp_path, capsys):
    fake = FakePublic()
    assert _history(tmp_path, "--top-n", "2", "--days", "1", client=fake) == 0
    assert {c[1] for c in fake.calls if c[0] == "candles"} == {"BTCUSDT", "ETHUSDT"}
    for argv in (["--days", "0"], ["--max-requests", "0"], ["--symbols", "x y!"]):
        assert _history(tmp_path, *argv, client=FakePublic()) == 2
    assert "refused" in capsys.readouterr().err


def test_history_aborts_on_repeated_failures_without_raising(tmp_path):
    class Down(FakePublic):
        def candles(self, *a):
            raise WallexHTTPError("down")

    assert _history(tmp_path, "--symbols", "BTCUSDT", "--days", "1", client=Down()) == 1


def test_record_sigterm_before_the_first_cycle_exits_zero_without_recording(tmp_path):
    """PID 1 in a container ignores signals without a handler: they must be installed before the universe GET."""
    class SlowRank(FakePublic):
        def rank_universe(self, quote, top_n):
            os.kill(os.getpid(), signal.SIGTERM)
            return super().rank_universe(quote, top_n)

    fake = SlowRank()
    assert collect.run_record(_args("record", "--out", str(tmp_path), "--top-n", "2"), client=fake) == 0
    assert [c for c in fake.calls if c[0] in ("depth", "trades")] == []


def test_history_sigterm_saves_progress_and_exits_one(tmp_path, capsys):
    class Term(FakePublic):
        def candles(self, symbol, resolution, start, end):
            if len([c for c in self.calls if c[0] == "candles"]) == 1:
                os.kill(os.getpid(), signal.SIGTERM)          # arrives while the second chunk is requested
            return super().candles(symbol, resolution, start, end)

    fake = Term()
    assert _history(tmp_path, "--symbols", "BTCUSDT", "--days", "1", client=fake) == 1
    assert "interrupted; progress saved" in capsys.readouterr().err
    last_closed = NOW // 60 * 60 - 60
    assert load_history(tmp_path, "BTCUSDT", last_closed - 86_400, last_closed)       # the finished chunk is on disk
    resume = FakePublic()
    assert _history(tmp_path, "--symbols", "BTCUSDT", "--days", "1", client=resume) == 0
    assert 1 <= len(resume.calls) <= 2                                                # only the missing chunks
