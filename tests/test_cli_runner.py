"""CLI smoke tests and the backtest runner: determinism, TestSetGuard discipline, final-test refusal."""
import functools
import json
import random
import statistics
from decimal import Decimal
from pathlib import Path

import pytest

from wallexbot import cli
from wallexbot.backtest import runner
from wallexbot.backtest.costs import CostModel
from wallexbot.backtest.splits import TestSetAlreadyUsed, TestSetGuard
from wallexbot.data.aggregate import aggregate_to
from wallexbot.data.candles import Candle, write_candles_csv
from wallexbot.strategy.config import MomentumParams
from wallexbot.strategy.contract import PositionState
from wallexbot.strategy.exits import ExitParams
from wallexbot.strategy.momentum import MomentumSignalEngine
from wallexbot.types import SymbolRules

D = Decimal
SYMS = ("AAAUSDT", "BBBUSDT")
T0 = 1_701_388_800 - 4 * 86400  # 2023-11-27 00:00 UTC (15m aligned): the data spans two month files
N_1M = 800 * 15  # 800 closed 15m bars


def synth_1m(seed: int, n: int = N_1M, start: int = T0, base: str = "100.00") -> list[Candle]:
    rng = random.Random(seed)
    px, out = D(base), []
    for i in range(n):
        drift = D("0.0004") if (i // 400) % 3 == 0 else D("-0.0001")  # alternating trend phases
        o = px
        c = max(D("1.00"), (o * (1 + drift + D(rng.randint(-8, 8)) / D(10000))).quantize(D("0.01")))
        spike = 1 + (rng.random() < 0.03) * 6
        out.append(Candle(start + 60 * i, o, max(o, c) + D("0.05"), min(o, c) - D("0.05"), c,
                          D(rng.randint(5, 20) * spike).quantize(D("0.001")), 0))
        px = c
    return out


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    d = tmp_path_factory.mktemp("history")
    for k, s in enumerate(SYMS):
        c = synth_1m(k + 1, base=str(100 + 40 * k) + ".00")
        if k == 1:
            del c[1000]  # one absent minute -> one flagged 15m bar
        write_candles_csv(d / s / f"{s}_1m_2023-11.csv", [x for x in c if x.ts < 1_701_388_800])
        rest = [x for x in c if x.ts >= 1_701_388_800]
        if rest:
            write_candles_csv(d / s / f"{s}_1m_2023-12.csv", rest)
    return d


def run_small(history, out, workers=1):
    return runner.run_grid(history, out, symbols=SYMS, workers=workers, sens_only=("volume_mult", "min_score"))


# ---------------------------------------------------------------- CLI
def test_demo_prints_a_round_trip(capsys):
    assert cli.main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "scripted mock round trip" in out and "realized P&L" in out and "FILLED" in out.upper()


def test_doctor_without_key_exits_2_and_sends_nothing(monkeypatch, capsys):
    monkeypatch.delenv("WALLEX_API_KEY_READ", raising=False)

    def no_doctor(*a, **k):
        raise AssertionError("run_doctor must not be called without a key")

    monkeypatch.setattr("wallexbot.doctor.run_doctor", no_doctor)
    assert cli.main(["doctor"]) == 2
    assert "WALLEX_API_KEY_READ" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--arm-live", "demo"], ["demo", "--arm-live"], ["backtest", "--arm-live"],
                                  ["guardian", "--arm-live"], ["doctor", "--arm-live=1"]])
def test_arm_live_is_refused_everywhere(argv, capsys):
    assert cli.main(argv) == 2
    assert "live is not available in this build" in capsys.readouterr().err


def test_arm_live_is_not_an_argparse_option():
    parser = cli.build_parser()
    for sub in parser._subparsers._group_actions[0].choices.values():
        assert not any("--arm-live" in a.option_strings for a in sub._actions)
    assert not any("--arm-live" in a.option_strings for a in parser._actions)


def test_guardian_delegates_and_refuses_live_mode(tmp_path, capsys):
    assert cli.main(["guardian", "--db", str(tmp_path / "g.db"), "--mode", "live"]) == 2
    assert "refused" in capsys.readouterr().err
    assert cli.main(["guardian", "--db", str(tmp_path / "g.db"), "--mode", "mock", "--ticks", "1",
                     "--interval", "0"]) == 0
    assert "guardian stopped: ticks=1" in capsys.readouterr().out


# ---------------------------------------------------------------- data, rules, split
def test_load_bars_flags_missing_minutes_and_rules_are_derived(history):
    bars, quality = runner.load_bars_15m(history, SYMS)
    assert quality["AAAUSDT"]["bars_with_missing_minutes"] == 0
    assert quality["BBBUSDT"]["bars_with_missing_minutes"] == 1
    assert quality["BBBUSDT"]["missing_minutes_in_bars"] == 1
    assert all(len(v) == 800 for v in bars.values())
    rules, src = runner.symbol_rules(SYMS, bars)
    assert src == {"AAAUSDT": "derived", "BBBUSDT": "derived"}
    assert rules["AAAUSDT"].price_precision == 2 and rules["AAAUSDT"].min_notional is None
    # the smallest precision whose lot is worth <= 0.15 USDT at the median price
    p = rules["AAAUSDT"].amount_precision
    med = D(str(statistics.median(float(c.close) for c in bars["AAAUSDT"])))
    assert D(1).scaleb(-p) * med <= D("0.15") < D(1).scaleb(-(p - 1)) * med


def test_symbol_rules_prefer_the_fixture(history):
    bars = {"BTCUSDT": synth_1m(1)[:30], "PEPEUSDT": synth_1m(2)[:30]}
    rules, src = runner.symbol_rules(("BTCUSDT", "PEPEUSDT"), bars)
    assert src == {"BTCUSDT": "fixture", "PEPEUSDT": "derived"}
    assert (rules["BTCUSDT"].amount_precision, rules["BTCUSDT"].price_precision) == (8, 2)


def test_split_has_embargo_and_ordering(history):
    bars, _ = runner.load_bars_15m(history, SYMS)
    s = runner.build_split(bars)
    assert s.embargo_bars == 300
    assert s.train.end_ts < s.validation.start_ts < s.validation.end_ts < s.test.start_ts
    assert s.validation.start - s.train.end == 300 and s.test.start - s.validation.end == 300


# ---------------------------------------------------------------- gate replica, exits
def test_cost_gate_replica_matches_the_engine(history):
    bars = aggregate_to(synth_1m(5, base="30.00"), "15m")
    p = MomentumParams()
    eng = MomentumSignalEngine(p)
    checked = 0
    for fee in ("0.0035", "0.001"):
        mk = CostModel(taker_rate=D(fee)).market_context()
        for i in range(150, 800, 7):
            d = eng.diagnose("AAAUSDT", bars[: i + 1], mk)
            if d is None:
                continue
            rule = next(r for r in d.rules if r.name == "cost_gate")
            assert rule.passed == runner.cost_gate_passes(d.features, p, mk)
            checked += 1
    assert checked > 100


def test_required_atr_pct_scales_with_fee():
    p = MomentumParams()
    hi = runner.required_atr_pct(p, CostModel(taker_rate=D("0.0035")).market_context(), favourable=True)
    lo = runner.required_atr_pct(p, CostModel(taker_rate=D("0.001")).market_context(), favourable=True)
    assert hi > lo and hi == pytest.approx(3 * 135 / (1.5 * 1e4))


def test_tick_exit_rounds_levels_down_and_keeps_identity():
    rules = {"AAAUSDT": SymbolRules("AAAUSDT", "AAA", "USDT", 3, 2)}
    e4 = runner.make_exit("E4", D("0.0035"), rules)
    e1 = runner.make_exit("E1", D("0.0035"), rules)
    st = PositionState("AAAUSDT", D("100.00"), 0, D("1"), D("98.00"), D("99.00"), D("101.00"), 0)
    tp = e4.targets(st)[0]
    assert tp.price == D("102.00") and tp.fraction == D("0.5")
    assert e1.targets(st)[0].price == D("103.00")
    assert e1.params != e4.params  # distinguishable in inputs_hash


def test_runner_cooldown_hook():
    class T:  # minimal stand-in for a closed Trade
        def __init__(self, sym, exit_ts, pnl):
            self.symbol, self.exit_ts, self.net_pnl = sym, exit_ts, D(pnl)

    cd = runner.RunnerCooldown()
    assert cd("AAAUSDT", 1000, ()) is False
    assert cd("AAAUSDT", 1000 + 7 * 900, [T("AAAUSDT", 1000, "-1")]) is True       # 8-bar wait after a loss
    assert cd("AAAUSDT", 1000 + 8 * 900, [T("AAAUSDT", 1000, "-1")]) is False
    assert cd("AAAUSDT", 1000 + 2 * 900, [T("AAAUSDT", 1000, "1")]) is False       # 2-bar wait after a win
    assert cd("BBBUSDT", 1000 + 900, [T("AAAUSDT", 1000, "-1")]) is False           # per symbol
    losses = [T("AAAUSDT", 1000 * i, "-1") for i in (1, 2, 3)]
    assert cd("BBBUSDT", 3000 + 3000, losses) is True                                # 3-loss streak, all symbols


# ---------------------------------------------------------------- grid run
@pytest.fixture(scope="module")
def grid_run(history, tmp_path_factory):
    out = tmp_path_factory.mktemp("rep1")
    rep = run_small(history, out)
    return out, rep


def test_grid_covers_everything_and_reports_costs(grid_run):
    out, rep = grid_run
    rows = rep["grid"]
    assert len(rows) == 2 * 2 * 4 * 3  # splits x strategies x exits x fees
    assert {r["split"] for r in rows} == {"train", "validation"}
    for r in rows:
        assert r["evaluations"] >= r["signals"] >= 0 and r["abstentions"] == r["evaluations"] - r["signals"]
        assert r["trade_count"] is not None and "fee_share_of_gross" in r and "events" in r
    assert [f["taker_rate"] for f in rep["fee_scenarios"]] == ["0.0035", "0.002", "0.001"]
    assert all("ASSUMED" in f["note"] for f in rep["fee_scenarios"][1:])
    assert len(rep["benchmark_buy_and_hold"]) == 6
    feas = rep["cost_gate_feasibility"]["per_symbol"]
    assert set(feas) == set(SYMS) and all(set(v["gate_pass"]) == {f["label"] for f in rep["fee_scenarios"]}
                                          for v in feas.values())
    assert rep["git_head"] is None or len(rep["git_head"]) == 40
    for name in ("backtest_grid.json", "backtest_grid.md", "backtest_top_train.json", "backtest_top_validation.md"):
        assert (out / name).is_file()
    top = json.loads((out / "backtest_top_validation.json").read_text())
    assert top["inputs_hash"] and top["result_hash"] and "git_sha" in top
    md = (out / "backtest_grid.md").read_text()
    assert "EXPLORATORY" in md and "RESERVED, not evaluated" in md and "ASSUMED fee scenario" in md


def test_runner_is_deterministic_and_worker_count_independent(history, grid_run, tmp_path):
    out1, _ = grid_run
    run_small(history, tmp_path / "again", workers=2)
    for name in ("backtest_grid.json", "backtest_grid.md", "backtest_top_train.json",
                 "backtest_top_validation.json"):
        assert (out1 / name).read_bytes() == (tmp_path / "again" / name).read_bytes(), name


def test_runner_never_consumes_the_test_set_guard(history, tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(runner, "LEDGER_DEFAULT", ledger)

    def boom(*a, **k):
        raise AssertionError("TestSetGuard touched by the grid runner")

    monkeypatch.setattr(TestSetGuard, "consume", boom)
    monkeypatch.setattr(TestSetGuard, "used", boom)
    monkeypatch.setattr(runner, "run_grid", functools.partial(runner.run_grid, sens_only=("min_score",)))
    assert cli.main(["backtest", "--history-dir", str(history), "--out-dir", str(tmp_path / "o"), "--symbols",
                     *SYMS, "--workers", "1"]) == 0
    assert not ledger.exists()
    # and the test segment is not part of any run: no bar after the validation segment is evaluated
    bars, _ = runner.load_bars_15m(history, SYMS)
    s = runner.build_split(bars)
    seg, _start = s.bars_for("validation", bars, runner.WARMUP_BARS)
    assert max(c.ts for cs in seg.values() for c in cs) == s.validation.end_ts < s.test.start_ts


# ---------------------------------------------------------------- final test
def frozen(tmp_path, **over):
    p = tmp_path / "frozen.json"
    p.write_text(json.dumps({"strategy": "S0", "exit": "E4", "taker_rate": "0.0035", **over}))
    return p


def test_final_test_refuses_without_params_file(history, tmp_path, capsys):
    ledger = tmp_path / "ledger.jsonl"
    with pytest.raises(runner.FrozenParamsError):
        runner.run_final_test(history, tmp_path / "o", None, ledger=ledger, symbols=SYMS)
    assert not ledger.exists()
    assert cli.main(["backtest", "--final-test", "--history-dir", str(history), "--out-dir", str(tmp_path / "o"),
                     "--ledger", str(ledger), "--symbols", *SYMS]) == 2
    assert "frozen.json" in capsys.readouterr().err and not ledger.exists()
    assert cli.main(["backtest", "--params-file", str(frozen(tmp_path))]) == 2  # params file alone is not enough


def test_invalid_params_file_does_not_burn_the_guard(history, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"strategy": "S9", "exit": "E4", "taker_rate": "0.0035"}))
    with pytest.raises(runner.FrozenParamsError):
        runner.run_final_test(history, tmp_path / "o", bad, ledger=ledger, symbols=SYMS)
    with pytest.raises(runner.FrozenParamsError):
        runner.run_final_test(history, tmp_path / "o", tmp_path / "missing.json", ledger=ledger, symbols=SYMS)
    assert not ledger.exists()


def test_final_test_consumes_the_guard_exactly_once(history, tmp_path, capsys):
    ledger, out = tmp_path / "ledger.jsonl", tmp_path / "final"
    p = frozen(tmp_path)
    rep = runner.run_final_test(history, out, p, ledger=ledger, symbols=SYMS)
    assert rep["split"] == "test" and (out / "backtest_final_test.json").is_file()
    assert len(ledger.read_text().splitlines()) == 1
    with pytest.raises(TestSetAlreadyUsed):
        runner.run_final_test(history, out, p, ledger=ledger, symbols=SYMS)
    assert len(ledger.read_text().splitlines()) == 1
    # the CLI maps the second use to a refusal (exit 3), not a traceback
    assert cli.main(["backtest", "--final-test", "--params-file", str(p), "--history-dir", str(history),
                     "--out-dir", str(out), "--ledger", str(ledger), "--symbols", *SYMS]) == 3
    assert "single-use" in capsys.readouterr().err


def test_frozen_params_are_validated():
    with pytest.raises(runner.FrozenParamsError):
        runner.load_frozen_params(Path("/nonexistent/frozen.json"))


def test_exit_params_defaults_unchanged():
    assert ExitParams().fee_rate == D("0.0035")  # the runner replaces it per fee scenario, never mutates it
