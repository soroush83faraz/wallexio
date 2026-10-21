"""Chronological splits, walk-forward windows, and the single-use test-set guard.

Layout on the sorted, unique bar timestamps:

    [ train ][ embargo ][ validation ][ embargo ][ test ]

The embargo must be at least the longest indicator lookback, so no bar of one segment can feed an
indicator value used for a decision in the next. When a segment is backtested, warm-up bars are
taken from the embargo before it (never from the previous segment), and entries start at the
segment's first bar (`BacktestParams.trade_start_ts`).

Every split refuses negative sizes and checks that its segments are disjoint and ordered
(train < validation < test, with at least `embargo_bars` between them).

The test set is used ONCE (docs/BACKTEST.md). `TestSetGuard` enforces that with an append-only
JSON-lines ledger: the second `consume()` of the same test set raises, across processes.

Test-set identity (`make_test_set_id`) is NOT the exact bar range: it is the dataset identity
(a caller-chosen name, e.g. the history MANIFEST's fetch id), the sorted symbols, and the nominal
test period as whole UTC days, each end rounded to the NEAREST midnight. Moving the window by a
bar (or a few hours) therefore gives the same id. On top of that the guard refuses any test set
whose day range OVERLAPS one already consumed for the same dataset and a common symbol, so a
shifted or resized window that reuses consumed test days is refused even when its id differs.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Mapping, Sequence

from wallexbot.data.candles import Candle

_DAY = 86400


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
        if warmup_bars < 0:
            raise ValueError("warmup_bars must be >= 0")
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


def _check_sizes(embargo_bars: int, max_lookback_bars: int) -> None:
    for name, v in (("embargo_bars", embargo_bars), ("max_lookback_bars", max_lookback_bars)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError(f"{name} must be an int >= 0, got {v!r}")
    if embargo_bars < max_lookback_bars:
        raise ValueError(f"embargo ({embargo_bars}) must be >= max lookback ({max_lookback_bars}) bars")


def _check_disjoint(embargo_bars: int, *segments: Segment) -> None:
    """Segments in order, non-empty, and separated by at least `embargo_bars` bars."""
    for a, b in zip(segments, segments[1:]):
        if b.start - a.end < embargo_bars or a.end > b.start:
            raise ValueError(f"segments {a.name} [{a.start},{a.end}) and {b.name} [{b.start},{b.end}) overlap "
                             f"or are closer than the {embargo_bars}-bar embargo")


def chronological_split(timestamps: Sequence[int], *, embargo_bars: int, max_lookback_bars: int,
                        train_frac: Decimal = Decimal("0.6"), val_frac: Decimal = Decimal("0.2")) -> Split:
    ts = _check_ts(timestamps)
    _check_sizes(embargo_bars, max_lookback_bars)
    if not (0 < train_frac and 0 < val_frac and train_frac + val_frac < 1):
        raise ValueError("fractions must be positive and leave room for a test segment")
    usable = len(ts) - 2 * embargo_bars
    if usable <= 0:
        raise ValueError("not enough bars for two embargoes")
    n_train = int((usable * train_frac).to_integral_value(ROUND_FLOOR))
    n_val = int((usable * val_frac).to_integral_value(ROUND_FLOOR))
    train = _seg("train", ts, 0, n_train)
    v0 = n_train + embargo_bars
    val = _seg("validation", ts, v0, v0 + n_val)
    t0 = val.end + embargo_bars
    test = _seg("test", ts, t0, len(ts))
    _check_disjoint(embargo_bars, train, val, test)
    return Split(train, val, test, embargo_bars, ts)


def walk_forward(timestamps: Sequence[int], *, train_bars: int, test_bars: int, embargo_bars: int,
                 max_lookback_bars: int, step_bars: int | None = None) -> list[tuple[Segment, Segment]]:
    """Rolling (train, test) windows separated by the embargo; step defaults to test_bars."""
    ts = _check_ts(timestamps)
    _check_sizes(embargo_bars, max_lookback_bars)
    if min(train_bars, test_bars) <= 0:
        raise ValueError("window sizes must be positive")
    if step_bars is not None and step_bars <= 0:
        raise ValueError("step_bars must be positive")
    step = step_bars or test_bars
    out, start = [], 0
    while start + train_bars + embargo_bars + test_bars <= len(ts):
        t0 = start + train_bars + embargo_bars
        pair = (_seg("train", ts, start, start + train_bars), _seg("test", ts, t0, t0 + test_bars))
        _check_disjoint(embargo_bars, *pair)
        out.append(pair)
        start += step
    return out


_ID_RE = re.compile(r"^ts1\|(?P<dataset>[^|]*)\|(?P<symbols>[^|]*)\|(?P<start>\d{4}-\d{2}-\d{2})\.\."
                    r"(?P<end>\d{4}-\d{2}-\d{2})$")


def _nearest_midnight(ts: int) -> int:
    return (ts + _DAY // 2) // _DAY * _DAY


def _iso_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def make_test_set_id(split: Split, symbols: Sequence[str], *, dataset: str = "unnamed") -> str:
    """`ts1|<dataset>|<symbols>|<first day>..<end day>`: the test period's first bar and last bar
    rounded to the nearest UTC midnight (end exclusive, at least one day). Stable under a shift of
    the window by less than half a day at either end; see the module docstring."""
    if "|" in dataset or any("|" in s or "," in s for s in symbols):
        raise ValueError("dataset and symbols must not contain '|' (or ',' in a symbol)")
    start = _nearest_midnight(split.test.start_ts)
    end = max(_nearest_midnight(split.test.end_ts), start + _DAY)
    return f"ts1|{dataset}|{','.join(sorted(set(symbols)))}|{_iso_day(start)}..{_iso_day(end)}"


def _parse_id(test_set_id: str) -> tuple[str, frozenset[str], str, str] | None:
    m = _ID_RE.match(test_set_id)
    if m is None:
        return None
    return m["dataset"], frozenset(filter(None, m["symbols"].split(","))), m["start"], m["end"]


def _overlaps(a: str, b: str) -> bool:
    """Same test id, or two structured ids of one dataset sharing a symbol and a test day."""
    if a == b:
        return True
    pa, pb = _parse_id(a), _parse_id(b)
    if pa is None or pb is None:
        return False
    return pa[0] == pb[0] and bool(pa[1] & pb[1]) and pa[2] < pb[3] and pb[2] < pa[3]


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
        """True if this test set, or one overlapping it (same dataset, a shared symbol, a shared
        test day), was already consumed."""
        if not self.path.exists():
            return False
        with self.path.open("r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            return any(_overlaps(str(r.get("test_set_id")), test_set_id) for r in self._records(f))

    def consume(self, test_set_id: str, note: str = "") -> dict:
        """Record the single use of `test_set_id`; raises TestSetAlreadyUsed on any later call for
        the same or an overlapping test set."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            for r in self._records(f):
                if _overlaps(str(r.get("test_set_id")), test_set_id):
                    raise TestSetAlreadyUsed(f"test set {test_set_id!r} overlaps {r.get('test_set_id')!r}, "
                                             f"already used at {r.get('used_at')}")
            rec = {"test_set_id": test_set_id, "used_at": int(time.time()), "note": note}
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
            return rec
