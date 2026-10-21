"""Resumable historical 1-minute candle downloader (public, unauthenticated GETs only).

Design:
- Walks BACKWARDS from the newest closed bar in chunks of 600 bars (exactly one `/v1/udf/history` request
  per chunk via `WallexPublicClient.candles`). Backwards is chosen because the useful (recent) data lands
  first and the exchange's retention floor is discovered by the first empty chunk (`no_data`), after which
  the walk for that symbol stops cleanly.
- Layout: `<out_dir>/<SYMBOL>/<SYMBOL>_1m_<YYYY-MM>.csv` (+ `.sha256` sidecars via `write_candles_csv`), one file
  per UTC month, plus `<out_dir>/<SYMBOL>/state.json` holding the merged list of bar-open ranges already
  ANSWERED by the API (data or no_data). Resuming computes `requested window - answered ranges` and fetches only
  that, so true exchange gaps and the pre-listing region are not re-requested on every run.
- Data is flushed to disk every few chunks and on any exit path, so an abort or a crash loses little.
- Bounded: per-chunk retries, abort after N consecutive failed chunks, and a hard request budget.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from wallexbot.data.candles import Candle, CandleDataError, read_candles_csv, sidecar_path, write_candles_csv
from wallexbot.data.quality import QualityReport, check_candles
from wallexbot.exchange.wallex_public import MAX_BARS_PER_REQUEST, WallexDataError, WallexHTTPError

STEP = 60  # 1m bars
CHUNK_BARS = MAX_BARS_PER_REQUEST  # 600 bars -> exactly one request per chunk
DEFAULT_MAX_REQUESTS = 1500


class HistoryError(RuntimeError):
    pass


@dataclass
class RequestBudget:
    """Shared across symbols so a whole run never exceeds `limit` chunk requests."""
    limit: int = DEFAULT_MAX_REQUESTS
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used


@dataclass(frozen=True)
class HistoryReport:
    symbol: str
    rows: int  # bars inside the requested window now on disk
    first_ts: int | None
    last_ts: int | None
    requested_start: int
    requested_end: int
    gaps: int  # number of gap runs
    missing_bars: int
    zero_volume_runs: int
    coverage: float  # present / expected over [first_ts, last_ts]
    window_coverage: float  # present / expected over the requested window
    requests: int  # chunk requests issued in THIS call
    chunks_ok: int
    history_floor_ts: int | None  # earliest bar the API holds, if no_data was hit before requested start
    reached_start: bool  # whole requested window is answered
    aborted: str | None  # reason if stopped early (failures / budget), else None
    files: dict = field(default_factory=dict)  # file name -> sha256
    quality: QualityReport | None = None


# ---- layout helpers --------------------------------------------------------
def _sym_dir(out_dir: Path | str, symbol: str) -> Path:
    return Path(out_dir) / symbol


def _month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def _month_file(out_dir: Path | str, symbol: str, key: str) -> Path:
    return _sym_dir(out_dir, symbol) / f"{symbol}_1m_{key}.csv"


def _month_range(key: str) -> tuple[int, int]:
    y, m = int(key[:4]), int(key[5:])
    lo = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return lo, int(datetime(ny, nm, 1, tzinfo=timezone.utc).timestamp()) - 1


def _months_between(start_ts: int, end_ts: int) -> list[str]:
    keys, ts = [], start_ts
    while ts <= end_ts:
        k = _month_key(ts)
        if k not in keys:
            keys.append(k)
        ts = _month_range(k)[1] + 1
    return keys


def _state_path(out_dir: Path | str, symbol: str) -> Path:
    return _sym_dir(out_dir, symbol) / "state.json"


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + STEP:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract(window: tuple[int, int], covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sub-ranges of `window` (inclusive, bar-open ts) not inside `covered` (merged, sorted)."""
    a, b = window
    out = []
    cur = a
    for ca, cb in covered:
        if cb < cur:
            continue
        if ca > b:
            break
        if ca > cur:
            out.append((cur, ca - STEP))
        cur = max(cur, cb + STEP)
        if cur > b:
            break
    if cur <= b:
        out.append((cur, b))
    return out


def _load_state(out_dir, symbol) -> dict:
    p = _state_path(out_dir, symbol)
    if not p.exists():
        return {"fetched": [], "floor_ts": None}
    try:
        raw = json.loads(p.read_text())
        return {"fetched": [(int(a), int(b)) for a, b in raw["fetched"]],
                "floor_ts": None if raw.get("floor_ts") is None else int(raw["floor_ts"])}
    except (ValueError, KeyError, TypeError) as e:
        raise HistoryError(f"corrupt state file {p}: {e}") from e


def _save_state(out_dir, symbol, state: dict) -> None:
    p = _state_path(out_dir, symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"fetched": [list(r) for r in state["fetched"]], "floor_ts": state["floor_ts"]}) + "\n")
    os.replace(tmp, p)


def _load_existing(out_dir, symbol) -> dict[int, Candle]:
    """All stored bars, checksum-verified (a mismatch raises CandleDataError: never silently re-use bad data)."""
    d = _sym_dir(out_dir, symbol)
    have: dict[int, Candle] = {}
    if d.is_dir():
        for p in sorted(d.glob(f"{symbol}_1m_*.csv")):
            for c in read_candles_csv(p, verify=True):
                have[c.ts] = c
    return have


def _flush(out_dir, symbol, have: dict[int, Candle], dirty: set[str]) -> None:
    by_month: dict[str, list[Candle]] = {}
    for k in dirty:
        lo, hi = _month_range(k)
        by_month[k] = [have[t] for t in sorted(have) if lo <= t <= hi]
    for k, rows in by_month.items():
        if rows:
            write_candles_csv(_month_file(out_dir, symbol, k), rows)
    dirty.clear()


# ---- public API ------------------------------------------------------------
def fetch_history(client, symbol: str, start_ts: int, end_ts: int, out_dir: Path | str, *,
                  max_requests: int = DEFAULT_MAX_REQUESTS, budget: RequestBudget | None = None,
                  chunk_sleep_s: float = 0.2, chunk_retries: int = 2, max_consecutive_failures: int = 3,
                  flush_every: int = 10, clock: Callable[[], float] = time.time,
                  sleep: Callable[[float], None] = time.sleep) -> HistoryReport:
    """Download closed 1m bars with open time in [start_ts, end_ts] into `out_dir`, resumably.

    Re-running appends only ranges the API has not yet answered. `end_ts` is clamped to the newest closed
    bar. If the API returns no_data before `start_ts` the walk for that range stops and the earliest
    available bar is reported as `history_floor_ts`.
    """
    if budget is None:
        budget = RequestBudget(max_requests)
    a = -(-int(start_ts) // STEP) * STEP
    b = min(int(end_ts) // STEP * STEP, (int(clock()) // STEP) * STEP - STEP)  # newest CLOSED bar
    if a > b:
        raise ValueError("empty window")
    have = _load_existing(out_dir, symbol)
    state = _load_state(out_dir, symbol)
    dirty: set[str] = set()
    missing = _subtract((a, b), _merge_ranges(state["fetched"]))
    requests = chunks_ok = consecutive_fail = since_flush = 0
    aborted: str | None = None

    def checkpoint() -> None:
        _flush(out_dir, symbol, have, dirty)
        state["fetched"] = _merge_ranges(state["fetched"])
        _save_state(out_dir, symbol, state)

    try:
        for ra, rb in reversed(missing):  # newest range first
            hi = rb
            while hi >= ra and aborted is None:
                lo = max(ra, hi - (CHUNK_BARS - 1) * STEP)
                if budget.remaining <= 0:
                    aborted = f"request budget of {budget.limit} exhausted"
                    break
                rows = None
                for attempt in range(chunk_retries + 1):
                    if attempt:
                        sleep(1.0 * 2 ** (attempt - 1))
                    budget.used += 1
                    requests += 1
                    try:
                        rows = client.candles(symbol, "1", lo, hi)
                        break
                    except (WallexHTTPError, WallexDataError, OSError, TimeoutError):
                        if budget.remaining <= 0:
                            break
                if rows is None:
                    consecutive_fail += 1
                    if consecutive_fail >= max_consecutive_failures:
                        aborted = f"{consecutive_fail} consecutive failed chunks (last window {lo}..{hi})"
                    elif budget.remaining <= 0:
                        aborted = f"request budget of {budget.limit} exhausted"
                    sleep(chunk_sleep_s * 5)
                    # leave the chunk unanswered; try the next older one (resume will pick it up later)
                    hi = lo - STEP
                    continue
                consecutive_fail = 0
                chunks_ok += 1
                for c in rows:
                    have[c.ts] = c
                    dirty.add(_month_key(c.ts))
                if rows:
                    state["fetched"].append((lo, hi))
                    since_flush += 1
                    hi = lo - STEP
                else:
                    # no_data: nothing exists at or before this chunk; the whole remainder of the range is answered
                    state["fetched"].append((ra, hi))
                    older = [t for t in have if t > hi]
                    if older:
                        state["floor_ts"] = min(older) if state["floor_ts"] is None else min(state["floor_ts"], min(older))
                    hi = ra - STEP  # stop this range
                    since_flush = flush_every
                if since_flush >= flush_every:
                    checkpoint()
                    since_flush = 0
                if hi >= ra:
                    sleep(chunk_sleep_s)
            if aborted:
                break
    finally:
        checkpoint()

    window = [have[t] for t in sorted(have) if a <= t <= b]
    q = check_candles(window, STEP)
    covered = _merge_ranges(state["fetched"])
    reached = not _subtract((a, b), covered)
    files = {}
    for k in _months_between(a, b):
        p = _month_file(out_dir, symbol, k)
        if p.exists():
            files[p.name] = sidecar_path(p).read_text().strip()
    expected = (b - a) // STEP + 1
    return HistoryReport(
        symbol=symbol, rows=len(window), first_ts=q.first_ts, last_ts=q.last_ts, requested_start=a, requested_end=b,
        gaps=len(q.gaps), missing_bars=q.missing_total, zero_volume_runs=len(q.zero_volume_runs),
        coverage=q.coverage, window_coverage=len(window) / expected, requests=requests, chunks_ok=chunks_ok,
        history_floor_ts=state["floor_ts"], reached_start=reached, aborted=aborted, files=files, quality=q)


def load_history(out_dir: Path | str, symbol: str, start_ts: int, end_ts: int) -> list[Candle]:
    """Read bars with open time in [start_ts, end_ts], oldest first. Every month file is checksum-verified;
    a missing sidecar or mismatch raises CandleDataError. Duplicate timestamps across files are rejected."""
    out: list[Candle] = []
    seen: set[int] = set()
    for k in _months_between(int(start_ts), int(end_ts)):
        p = _month_file(out_dir, symbol, k)
        if not p.exists():
            continue
        for c in read_candles_csv(p, verify=True):
            if c.ts in seen:
                raise CandleDataError(f"duplicate ts {c.ts} in {p}")
            seen.add(c.ts)
            if start_ts <= c.ts <= end_ts:
                out.append(c)
    out.sort(key=lambda c: c.ts)
    return out
