"""Runner v2: --cost-source / --bar-minutes / --report-prefix flags, 60m segments built from the 15m split,
per-symbol recorded costs with proxy fallback, and the TestSetGuard is never touched."""
import json
import random
from decimal import Decimal
from pathlib import Path

import pytest

from wallexbot import cli
from wallexbot.backtest import runner
from wallexbot.backtest.calibrate import MIN_SAMPLES, calibrate_costs
from wallexbot.backtest.splits import TestSetGuard
from wallexbot.data.candles import Candle, write_candles_csv

D = Decimal
SYMS = ("AAAUSDT", "BBBUSDT")
T0 = 1_701_388_800 - 4 * 86400
N_1M = 800 * 15


def synth_1m(seed: int, base: str) -> list[Candle]:
    rng = random.Random(seed)
    px, out = D(base), []
    for i in range(N_1M):
        drift = D("0.0004") if (i // 400) % 3 == 0 else D("-0.0001")
        o = px
        c = max(D("1.00"), (o * (1 + drift + D(rng.randint(-8, 8)) / D(10000))).quantize(D("0.01")))
        spike = 1 + (rng.random() < 0.03) * 6
        out.append(Candle(T0 + 60 * i, o, max(o, c) + D("0.05"), min(o, c) - D("0.05"), c,
                          D(rng.randint(5, 20) * spike).quantize(D("0.001")), 0))
        px = c
    return out


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    d = tmp_path_factory.mktemp("history_v2")
    for k, s in enumerate(SYMS):
        c = synth_1m(k + 11, str(100 + 40 * k) + ".00")
        write_candles_csv(d / s / f"{s}_1m_2023-11.csv", [x for x in c if x.ts < 1_701_388_800])
        write_candles_csv(d / s / f"{s}_1m_2023-12.csv", [x for x in c if x.ts >= 1_701_388_800])
    return d


@pytest.fixture(scope="module")
def calibration(tmp_path_factory):
    """A usable calibration for AAAUSDT only (BBBUSDT must fall back to the proxy, labelled)."""
    d = tmp_path_factory.mktemp("cal")
    book = {"bids": [["99.98", "20"], ["99.9", "200"]], "asks": [["100.02", "20"], ["100.1", "200"]]}
    rows = [{"ts": 1_792_622_400 + 30 * i, "symbol": "AAAUSDT", **book} for i in range(MIN_SAMPLES + 10)]
    f = d / "depth_20261021.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    cal = calibrate_costs([f], SYMS)
    return cal, cal.save(d / "cost_calibration.json")


def test_flag_parsing():
    p = cli.build_parser()
    a = p.parse_args(["backtest", "--cost-source", "recorded", "--bar-minutes", "15", "60", "--report-prefix", "x_",
                      "--calibration", "c.json", "--skip-sensitivity"])
    assert (a.cost_source, a.bar_minutes, a.report_prefix, a.calibration, a.skip_sensitivity) == \
        ("recorded", [15, 60], "x_", Path("c.json"), True)
    d = p.parse_args(["backtest"])
    assert (d.cost_source, d.bar_minutes, d.report_prefix, d.calibration) == (None, [15], "v2_", None)
    for bad in (["--bar-minutes", "30"], ["--cost-source", "volume"]):
        with pytest.raises(SystemExit):
            p.parse_args(["backtest", *bad])
    c = p.parse_args(["calibrate-costs", "--recorder-dir", "r", "--notionals", "100", "200"])
    assert (c.recorder_dir, c.notionals) == (Path("r"), [100, 200])


def test_recorded_without_calibration_is_refused(history, tmp_path, capsys):
    rc = cli.main(["backtest", "--history-dir", str(history), "--out-dir", str(tmp_path), "--symbols", *SYMS,
                   "--cost-source", "recorded", "--calibration", str(tmp_path / "missing.json")])
    assert rc == 2 and "calibration" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_segment_bars_15m_equals_split_bars_for_and_60m_stays_inside(history):
    by_tf, _ = runner.load_bars_tf(history, SYMS, ("15m", "1h"))
    split = runner.build_split(by_tf["15m"])
    for name in ("train", "validation"):
        assert runner.segment_bars(split, name, by_tf["15m"], 900) == \
            split.bars_for(name, by_tf["15m"], runner.WARMUP_BARS)
        seg = split.segment(name)
        bars60, start = runner.segment_bars(split, name, by_tf["1h"], 3600)
        lo = split.timestamps[max(0, seg.start - runner.WARMUP_BARS)]
        assert start == seg.start_ts
        for cs in bars60.values():
            assert cs and all(c.ts >= lo and c.ts + 3600 <= seg.end_ts + 900 and c.ts % 3600 == 0 for c in cs)
            assert max(c.ts for c in cs) + 3600 <= split.test.start_ts
    with pytest.raises(ValueError):
        runner.segment_bars(split, "test", by_tf["1h"], 3600)


def test_build_costs_labels_and_fallback(calibration):
    cal, _ = calibration
    book, labels = runner.build_costs(SYMS, "recorded", cal)
    assert labels == {"AAAUSDT": "recorded (p90)", "BBBUSDT": "proxy (no usable calibration)"}
    assert book.for_symbol("AAAUSDT").cost_source == "recorded" and book.for_symbol("BBBUSDT").cost_source == "proxy"
    proxy, plabels = runner.build_costs(SYMS, "proxy", cal)
    assert set(plabels.values()) == {"proxy (requested)"} and proxy.per_symbol == ()
    with pytest.raises(ValueError):
        runner.build_costs(SYMS, "recorded", None)


def test_v2_cli_run_never_consumes_the_guard(history, calibration, tmp_path, monkeypatch):
    _, cal_path = calibration
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(runner, "LEDGER_DEFAULT", ledger)

    def boom(*a, **k):
        raise AssertionError("TestSetGuard touched by the grid runner")

    monkeypatch.setattr(TestSetGuard, "consume", boom)
    monkeypatch.setattr(TestSetGuard, "used", boom)
    out = tmp_path / "o"
    assert cli.main(["backtest", "--history-dir", str(history), "--out-dir", str(out), "--symbols", *SYMS,
                     "--workers", "1", "--calibration", str(cal_path), "--bar-minutes", "15", "60",
                     "--skip-sensitivity"]) == 0           # cost source defaults to recorded: calibration exists
    assert not ledger.exists()
    names = {p.name for p in out.iterdir()}
    for stem in ("v2_backtest_grid_15m", "v2_backtest_grid_60m", "v2_backtest_top_15m_train",
                 "v2_backtest_top_60m_validation"):
        assert {f"{stem}.json", f"{stem}.md"} <= names
    assert "v2_summary.md" in names and "backtest_grid.json" not in names
    for minutes in (15, 60):
        rep = json.loads((out / f"v2_backtest_grid_{minutes}m.json").read_text())
        assert rep["bar_minutes"] == minutes and rep["cost_source"] == "recorded"
        assert rep["costs"]["labels"] == {"AAAUSDT": "recorded (p90)", "BBBUSDT": "proxy (no usable calibration)"}
        assert rep["costs"]["calibration"]["sha256"] == calibration[0].sha256
        assert len(rep["grid"]) == 2 * 2 * 4 * 3 and {g["split"] for g in rep["grid"]} == {"train", "validation"}
        for g in rep["grid"]:
            assert g["low_n"] == (g["trade_count"] < 30) and "fill_impact_status" in g
            assert set(g["fill_impact_status"]) <= {"proxy", "below_range", "interpolated", "extrapolated", "capped"}
        feas = rep["cost_gate_feasibility"]["per_symbol"]
        assert feas["AAAUSDT"]["cost_source"] == "recorded" and feas["BBBUSDT"]["cost_source"] == "proxy"
        split = rep["inputs"]["split"]
        assert split["test"]["start_ts"] > split["validation"]["end_ts"]
    md = (out / "v2_summary.md").read_text()
    assert "EXPLORATORY" in md and "Evidence count" in md and "60m" in md and "15m" in md
    grid_md = (out / "v2_backtest_grid_60m.md").read_text()
    assert "Cost model per symbol" in grid_md and "low statistical power" in grid_md


def test_recorded_60m_run_is_deterministic_and_worker_independent(history, calibration, tmp_path):
    cal, cal_path = calibration
    kw = dict(symbols=SYMS, sens_only=(), bar_minutes=60, cost_source="recorded", calibration=cal,
              report_prefix="v2_", calibration_path=cal_path)
    runner.run_grid(history, tmp_path / "a", workers=1, **kw)
    runner.run_grid(history, tmp_path / "b", workers=2, **kw)
    for name in ("v2_backtest_grid_60m.json", "v2_backtest_grid_60m.md", "v2_backtest_top_60m_train.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


def test_legacy_call_is_unchanged(history, tmp_path):
    rep = runner.run_grid(history, tmp_path, symbols=SYMS, workers=1, sens_only=())
    assert rep["cost_source"] == "proxy" and rep["bar_minutes"] == 15
    assert (tmp_path / "backtest_grid.json").is_file() and (tmp_path / "backtest_top_train.md").is_file()
    assert set(rep["costs"]["labels"].values()) == {"proxy (requested)"}
