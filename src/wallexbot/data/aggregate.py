"""Aggregate 1m candles into coarser bars using CLOSED buckets only (look-ahead safe)."""
from __future__ import annotations

from decimal import Decimal

from wallexbot.data.candles import Candle, CandleDataError

BASE_S = 60
TIMEFRAMES = {"15m": 15, "1h": 60, "4h": 240}  # name -> minutes


def aggregate(candles_1m: list[Candle], bucket_minutes: int, now: float | None = None) -> list[Candle]:
    """Aggregate strictly-increasing 1m candles into UTC-epoch-aligned buckets.

    A bucket [b, b+W) is emitted only when fully past: with `now`, when b+W <= now; without it,
    when the input shows it ended (a later candle exists, or the last 1m candle is the bucket's
    final minute). open=first, high=max, low=min, close=last, volume=sum; `missing` counts absent
    minutes inside the bucket. Empty buckets are not emitted (see data.quality for gaps).
    """
    if not isinstance(bucket_minutes, int) or bucket_minutes < 1:
        raise ValueError("bucket_minutes must be a positive integer")
    width = bucket_minutes * BASE_S
    prev = None
    for c in candles_1m:
        if c.ts % BASE_S:
            raise CandleDataError(f"1m candle not aligned to the minute: {c.ts}")
        if prev is not None and c.ts <= prev:
            raise CandleDataError(f"timestamps not strictly increasing at {c.ts}")
        prev = c.ts
    if not candles_1m:
        return []
    observed_until = candles_1m[-1].ts + BASE_S  # data covers everything before this instant

    buckets: dict[int, list[Candle]] = {}
    for c in candles_1m:
        buckets.setdefault(c.ts - c.ts % width, []).append(c)

    out: list[Candle] = []
    for start, group in buckets.items():
        closed = (start + width <= now) if now is not None else (start + width <= observed_until)
        if not closed:
            continue
        out.append(Candle(
            ts=start,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum((c.volume for c in group), Decimal(0)),
            missing=bucket_minutes - len(group),
        ))
    return out


def aggregate_to(candles_1m: list[Candle], timeframe: str, now: float | None = None) -> list[Candle]:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {timeframe!r}; use one of {sorted(TIMEFRAMES)}")
    return aggregate(candles_1m, TIMEFRAMES[timeframe], now)
