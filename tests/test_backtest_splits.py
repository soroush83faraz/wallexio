import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from wallexbot.backtest.sensitivity import run_sensitivity
from wallexbot.backtest.splits import (TestSetAlreadyUsed, TestSetGuard, chronological_split, make_test_set_id,
                                       walk_forward)
from wallexbot.data.candles import Candle

D = Decimal
TS = [i * 900 for i in range(100)]


def test_chronological_split_with_embargo_hand_computed():
    s = chronological_split(TS, embargo_bars=5, max_lookback_bars=5)
    # usable = 100 - 2*5 = 90 -> train 54, validation 18, test the rest
    assert (s.train.start, s.train.end) == (0, 54)
    assert (s.validation.start, s.validation.end) == (59, 77)
    assert (s.test.start, s.test.end) == (82, 100)
    assert s.validation.start_ts - s.train.end_ts == 6 * 900          # 5 embargo bars in between
    assert s.test.end_ts == TS[-1]


def test_embargo_must_cover_lookback_and_input_checks():
    with pytest.raises(ValueError, match="embargo"):
        chronological_split(TS, embargo_bars=4, max_lookback_bars=5)
    with pytest.raises(ValueError):
        chronological_split([0, 900, 900], embargo_bars=0, max_lookback_bars=0)
    with pytest.raises(ValueError):
        chronological_split(TS, embargo_bars=5, max_lookback_bars=5, train_frac=D("0.8"), val_frac=D("0.2"))


def test_bars_for_uses_embargo_warmup_only():
    s = chronological_split(TS, embargo_bars=5, max_lookback_bars=5)
    bars = {"A": [Candle(t, D(1), D(1), D(1), D(1), D(1)) for t in TS]}
    sub, start = s.bars_for("validation", bars, warmup_bars=5)
    assert start == TS[59] and sub["A"][0].ts == TS[54] and sub["A"][-1].ts == TS[76]
    assert sub["A"][0].ts > s.train.end_ts                              # no train bar leaks in
    with pytest.raises(ValueError):
        s.bars_for("validation", bars, warmup_bars=6)


def test_walk_forward_windows():
    w = walk_forward(TS, train_bars=40, test_bars=10, embargo_bars=5, max_lookback_bars=5)
    assert [(a.start, a.end, b.start, b.end) for a, b in w] == [
        (0, 40, 45, 55), (10, 50, 55, 65), (20, 60, 65, 75), (30, 70, 75, 85), (40, 80, 85, 95)]
    with pytest.raises(ValueError):
        walk_forward(TS, train_bars=40, test_bars=10, embargo_bars=1, max_lookback_bars=5)


def test_test_set_guard_is_single_use_and_persistent(tmp_path):
    ledger = tmp_path / "ledger" / "test_set.jsonl"
    s = chronological_split(TS, embargo_bars=5, max_lookback_bars=5)
    tid = make_test_set_id(s, ["BBBUSDT", "AAAUSDT"], dataset="hist-v1")
    # test bars 82..99 at 900 s: 73800..89100 s -> nearest midnights 86400 (1970-01-02) and 86400 -> 1 day
    assert tid == "ts1|hist-v1|AAAUSDT,BBBUSDT|1970-01-02..1970-01-03"
    g = TestSetGuard(ledger)
    assert not g.used(tid)
    rec = g.consume(tid, note="E4 frozen")
    assert rec["test_set_id"] == tid and g.used(tid)
    with pytest.raises(TestSetAlreadyUsed):
        g.consume(tid)
    with pytest.raises(TestSetAlreadyUsed):            # a fresh process/instance reads the same ledger
        TestSetGuard(ledger).consume(tid, note="retry")
    TestSetGuard(ledger).consume("other")              # a different test set is independent
    lines = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert [x["test_set_id"] for x in lines] == [tid, "other"]


def test_negative_embargo_or_lookback_and_overlap_are_refused():
    """Verifier: embargo_bars=-20 with max_lookback_bars=-20 was accepted and produced train
    (0, 624) overlapping validation (604, 812)."""
    ts = [i * 900 for i in range(1000)]
    with pytest.raises(ValueError, match="embargo_bars"):
        chronological_split(ts, embargo_bars=-20, max_lookback_bars=-20)
    with pytest.raises(ValueError, match="max_lookback_bars"):
        chronological_split(ts, embargo_bars=5, max_lookback_bars=-1)
    with pytest.raises(ValueError):
        walk_forward(ts, train_bars=40, test_bars=10, embargo_bars=-5, max_lookback_bars=-5)
    with pytest.raises(ValueError, match="step_bars"):
        walk_forward(ts, train_bars=40, test_bars=10, embargo_bars=5, max_lookback_bars=5, step_bars=-10)
    with pytest.raises(ValueError):
        chronological_split(ts[:10], embargo_bars=5, max_lookback_bars=5)          # no room for embargoes
    s = chronological_split(ts, embargo_bars=20, max_lookback_bars=20)
    assert s.train.end + 20 <= s.validation.start and s.validation.end + 20 <= s.test.start
    with pytest.raises(ValueError):
        s.bars_for("test", {"A": []}, warmup_bars=-1)


DAY = 86400


def test_test_set_id_ignores_a_one_bar_shift_and_guard_refuses_overlap(tmp_path):
    from wallexbot.backtest.splits import Segment, Split
    d0 = 20_000 * DAY                                                   # a UTC midnight

    def split(start, end):
        seg = Segment("test", 0, 1, start, end)
        return Split(seg, seg, seg, 0, (start, end))

    base = make_test_set_id(split(d0, d0 + 30 * DAY - 900), ["BTCUSDT", "ETHUSDT"], dataset="m1")
    for shift in (-900, 900, -2 * 900, 3 * 3600):                       # a bar or a few hours either way
        assert make_test_set_id(split(d0 + shift, d0 + 30 * DAY - 900 + shift), ["ETHUSDT", "BTCUSDT"],
                                dataset="m1") == base
    assert base == "ts1|m1|BTCUSDT,ETHUSDT|2024-10-04..2024-11-03"
    assert make_test_set_id(split(d0, d0 + 30 * DAY - 900), ["BTCUSDT"], dataset="other") != base

    g = TestSetGuard(tmp_path / "ledger.jsonl")
    g.consume(base)
    # an id that differs (window moved by 3 days) but reuses consumed test days is refused
    moved = make_test_set_id(split(d0 + 3 * DAY, d0 + 33 * DAY), ["BTCUSDT"], dataset="m1")
    assert moved != base and g.used(moved)
    with pytest.raises(TestSetAlreadyUsed):
        g.consume(moved)
    # disjoint days, another dataset, or no common symbol are independent test sets
    g.consume(make_test_set_id(split(d0 + 30 * DAY, d0 + 40 * DAY), ["BTCUSDT"], dataset="m1"))
    g.consume(make_test_set_id(split(d0, d0 + 30 * DAY), ["BTCUSDT"], dataset="m2"))
    g.consume(make_test_set_id(split(d0, d0 + 30 * DAY), ["SOLUSDT"], dataset="m1"))


@dataclass(frozen=True)
class P:
    lookback: int = 10
    mult: Decimal = D("1.0")
    flag: bool = True
    name: str = "x"


def test_sensitivity_grid_hand_computed():
    calls = []

    def run_fn(p):
        calls.append(p)
        return p.mult - D("0.9")              # expectancy: positive at 1.0, negative at 0.8

    r = run_sensitivity(run_fn, P(), split="validation")
    got = [(n.param, n.direction, n.value, n.same_sign) for n in r.neighbours]
    assert got == [("lookback", "-", 8, True), ("lookback", "+", 12, True),
                   ("mult", "-", D("0.80"), False), ("mult", "+", D("1.20"), True)]
    assert r.base_expectancy == D("0.1") and r.fraction_same_sign == 0.75
    assert len(calls) == 5                    # base + 4 neighbours; bool/str untouched


def test_sensitivity_refuses_test_split_and_handles_no_edge():
    with pytest.raises(ValueError, match="never"):
        run_sensitivity(lambda p: D(1), P(), split="test")
    r = run_sensitivity(lambda p: None, P(), split="train")
    assert r.fraction_same_sign is None and not any(n.same_sign for n in r.neighbours)
