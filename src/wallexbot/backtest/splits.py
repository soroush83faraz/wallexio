"""Chronological splits, walk-forward windows, and the single-use test-set guard.

Layout on the sorted, unique bar timestamps:

    [ train ][ embargo ][ validation ][ embargo ][ test ]

The embargo must be at least the longest indicator lookback, so no bar of one segment can feed an
indicator value used for a decision in the next. When a segment is backtested, warm-up bars are
taken from the embargo before it (never from the previous segment), and entries start at the
segment's first bar (`BacktestParams.trade_start_ts`).

The test set is used ONCE (docs/BACKTEST.md). `TestSetGuard` enforces that with an append-only
JSON-lines ledger: the second `consume()` of the same test-set id raises, across processes.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Mapping, Sequence

from wallexbot.data.candles import Candle


@dataclass(frozen=True)
class Segment:
    name: str
    start: int     # index into the timestamp list, inclusive
    end: int       # exclusive
    start_ts: int
    end_ts: int    # timestamp of the LAST bar in the segment (inclusive)

    def __len__(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Split:
    train: Segment
    validation: Segment
    test: Segment
    embargo_bars: int
    timestamps: tuple[int, ...]

    def segment(self, name: str) -> Segment:
        return {"train": self.train, "validation": self.validation, "test": self.test}[name]

    def bars_for(self, name: str, bars_by_symbol: Mapping[str, Sequence[Candle]],
                 warmup_bars: int) -> tuple[dict[str, list[Candle]], int]:
        """Bars of one segment plus up to `warmup_bars` earlier bars from the embargo, and the
        trade_start_ts to pass to BacktestParams."""
        if warmup_bars > self.embargo_bars:
            raise ValueError("warm-up longer than the embargo would leak the previous segment")
        seg = self.segment(name)
        lo = self.timestamps[max(0, seg.start - warmup_bars)]
        return ({s: [c for c in bs if lo <= c.ts <= seg.end_ts] for s, bs in sorted(bars_by_symbol.items())},
                seg.start_ts)


def _seg(name: str, ts: Sequence[int], start: int, end: int) -> Segment:
    if end <= start:
        raise ValueError(f"segment {name!r} is empty")
    return Segment(name, start, end, ts[start], ts[end - 1])


def _check_ts(timestamps: Sequence[int]) -> tuple[int, ...]:
    ts = tuple(timestamps)
    if any(b <= a for a, b in zip(ts, ts[1:])):
        raise ValueError("timestamps must be strictly increasing")
    return ts


def chronological_split(timestamps: Sequence[int], *, embargo_bars: int, max_lookback_bars: int,
                        train_frac: Decimal = Decimal("0.6"), val_frac: Decimal = Decimal("0.2")) -> Split:
    ts = _check_ts(timestamps)
    if embargo_bars < max_lookback_bars:
        raise ValueError(f"embargo ({embargo_bars}) must be >= max lookback ({max_lookback_bars}) bars")
    if not (0 < train_frac and 0 < val_frac and train_frac + val_frac < 1):
        raise ValueError("fractions must be positive and leave room for a test segment")
    usable = len(ts) - 2 * embargo_bars
    n_train = int((usable * train_frac).to_integral_value(ROUND_FLOOR))
    n_val = int((usable * val_frac).to_integral_value(ROUND_FLOOR))
    train = _seg("train", ts, 0, n_train)
    v0 = n_train + embargo_bars
    val = _seg("validation", ts, v0, v0 + n_val)
    t0 = val.end + embargo_bars
    test = _seg("test", ts, t0, len(ts))
    return Split(train, val, test, embargo_bars, ts)


def walk_forward(timestamps: Sequence[int], *, train_bars: int, test_bars: int, embargo_bars: int,
                 max_lookback_bars: int, step_bars: int | None = None) -> list[tuple[Segment, Segment]]:
    """Rolling (train, test) windows separated by the embargo; step defaults to test_bars."""
    ts = _check_ts(timestamps)
    if embargo_bars < max_lookback_bars:
        raise ValueError(f"embargo ({embargo_bars}) must be >= max lookback ({max_lookback_bars}) bars")
    if min(train_bars, test_bars) <= 0:
        raise ValueError("window sizes must be positive")
    step = step_bars or test_bars
    out, start = [], 0
    while start + train_bars + embargo_bars + test_bars <= len(ts):
        t0 = start + train_bars + embargo_bars
        out.append((_seg("train", ts, start, start + train_bars), _seg("test", ts, t0, t0 + test_bars)))
        start += step
    return out


def make_test_set_id(split: Split, symbols: Sequence[str]) -> str:
    return f"{','.join(sorted(symbols))}|{split.test.start_ts}|{split.test.end_ts}"


class TestSetAlreadyUsed(RuntimeError):
    __test__ = False


class TestSetGuard:
    """Single-use gate for the test set, backed by a persistent JSON-lines ledger file."""
    __test__ = False  # not a pytest test class

    def __init__(self, ledger_path: Path | str):
        self.path = Path(ledger_path)

    def _records(self, f) -> list[dict]:
        f.seek(0)
        return [json.loads(line) for line in f.read().splitlines() if line.strip()]

    def used(self, test_set_id: str) -> bool:
        if not self.path.exists():
            return False
        with self.path.open("r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            return any(r.get("test_set_id") == test_set_id for r in self._records(f))

    def consume(self, test_set_id: str, note: str = "") -> dict:
        """Record the single use of `test_set_id`; raises TestSetAlreadyUsed on any later call."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            for r in self._records(f):
                if r.get("test_set_id") == test_set_id:
                    raise TestSetAlreadyUsed(f"test set {test_set_id!r} already used at {r.get('used_at')}")
            rec = {"test_set_id": test_set_id, "used_at": int(time.time()), "note": note}
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
            return rec
