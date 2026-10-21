"""Candle data-quality checks for a 24/7 market (expected grid has no holidays or weekends)."""
from __future__ import annotations

from dataclasses import dataclass, field

from wallexbot.data.candles import Candle, CandleDataError


@dataclass(frozen=True)
class QualityReport:
    n: int
    step_s: int
    first_ts: int | None
    last_ts: int | None
    gaps: tuple[tuple[int, int], ...] = ()  # (first missing ts, number of missing bars)
    duplicates: tuple[int, ...] = ()  # ts seen again
    out_of_order: tuple[int, ...] = ()  # ts lower than an earlier ts
    misaligned: tuple[int, ...] = ()  # ts not on the step grid
    zero_volume_runs: tuple[tuple[int, int], ...] = ()  # (start ts, run length) with length >= zero_run_min
    notes: list[str] = field(default_factory=list, compare=False)

    @property
    def missing_total(self) -> int:
        return sum(m for _, m in self.gaps)

    @property
    def strictly_increasing(self) -> bool:
        return not self.duplicates and not self.out_of_order

    @property
    def coverage(self) -> float:
        """Fraction of the expected grid present (informational; not money)."""
        expected = self.n + self.missing_total
        return 1.0 if expected == 0 else self.n / expected

    @property
    def ok(self) -> bool:
        """Hard-clean: ordered, aligned and gap-free. Zero-volume runs are reported, not fatal."""
        return self.strictly_increasing and not self.gaps and not self.misaligned


def check_candles(candles: list[Candle], step_s: int = 60, zero_run_min: int = 5) -> QualityReport:
    duplicates: list[int] = []
    out_of_order: list[int] = []
    misaligned: list[int] = []
    gaps: list[tuple[int, int]] = []
    zero_runs: list[tuple[int, int]] = []
    run_start, run_len = 0, 0
    max_ts = None
    for c in candles:
        if c.ts % step_s:
            misaligned.append(c.ts)
        if max_ts is not None:
            if c.ts == max_ts:
                duplicates.append(c.ts)
            elif c.ts < max_ts:
                out_of_order.append(c.ts)
            elif c.ts - max_ts > step_s:
                gaps.append((max_ts + step_s, (c.ts - max_ts) // step_s - 1))
        max_ts = c.ts if max_ts is None else max(max_ts, c.ts)
        if c.volume == 0:
            if run_len == 0:
                run_start = c.ts
            run_len += 1
        else:
            if run_len >= zero_run_min:
                zero_runs.append((run_start, run_len))
            run_len = 0
    if run_len >= zero_run_min:
        zero_runs.append((run_start, run_len))
    return QualityReport(
        n=len(candles), step_s=step_s,
        first_ts=candles[0].ts if candles else None, last_ts=max_ts,
        gaps=tuple(gaps), duplicates=tuple(duplicates), out_of_order=tuple(out_of_order),
        misaligned=tuple(misaligned), zero_volume_runs=tuple(zero_runs),
    )


def require_strictly_increasing(candles: list[Candle]) -> None:
    """Raise unless timestamps are strictly increasing (used before any indicator math)."""
    for a, b in zip(candles, candles[1:]):
        if b.ts <= a.ts:
            raise CandleDataError(f"timestamps not strictly increasing: {a.ts} then {b.ts}")
