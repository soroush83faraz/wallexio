"""Candle value type and a CSV cache with a sha256 sidecar. Decimal only; never float."""
from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from wallexbot.types import D

COLUMNS = ("ts", "open", "high", "low", "close", "volume", "missing")


class CandleDataError(ValueError):
    pass


@dataclass(frozen=True)
class Candle:
    ts: int  # bar OPEN time, unix seconds UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # base-asset volume; semantics unverified, prefer relative volume
    missing: int = 0  # number of source bars absent inside this (aggregated) bar

    def __post_init__(self):
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise CandleDataError(f"inconsistent OHLC at ts={self.ts}")
        if self.low <= 0 or self.volume < 0:
            raise CandleDataError(f"non-positive price or negative volume at ts={self.ts}")


def sidecar_path(path: Path | str) -> Path:
    p = Path(path)
    return p.with_name(p.name + ".sha256")


def _fmt(d: Decimal) -> str:
    return format(d, "f")


def write_candles_csv(path: Path | str, candles: list[Candle]) -> str:
    """Atomically write candles and `<path>.sha256`. Returns the hex digest."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(COLUMNS)
        for c in candles:
            w.writerow([c.ts, _fmt(c.open), _fmt(c.high), _fmt(c.low), _fmt(c.close), _fmt(c.volume), c.missing])
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    os.replace(tmp, p)
    sidecar_path(p).write_text(digest + "\n")
    return digest


def read_candles_csv(path: Path | str, verify: bool = True) -> list[Candle]:
    """Read a cached file. With verify (default) a missing/mismatching sidecar raises."""
    p = Path(path)
    raw = p.read_bytes()
    if verify:
        sc = sidecar_path(p)
        if not sc.exists():
            raise CandleDataError(f"missing checksum sidecar for {p}")
        if sc.read_text().strip() != hashlib.sha256(raw).hexdigest():
            raise CandleDataError(f"checksum mismatch for {p}")
    rows = list(csv.reader(raw.decode().splitlines()))
    if not rows or tuple(rows[0]) != COLUMNS:
        raise CandleDataError(f"unexpected header in {p}")
    try:
        return [Candle(int(r[0]), D(r[1]), D(r[2]), D(r[3]), D(r[4]), D(r[5]), int(r[6])) for r in rows[1:]]
    except (ValueError, IndexError, ArithmeticError) as e:
        raise CandleDataError(f"corrupt row in {p}: {e}") from e
