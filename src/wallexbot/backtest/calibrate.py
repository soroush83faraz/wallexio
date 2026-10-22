"""Execution-cost calibration from recorded order books (`data/recorder.py` depth_YYYYMMDD.jsonl).

For every valid snapshot of a symbol (both sides non-empty, best ask > best bid):

    mid          = (best_bid + best_ask) / 2
    spread_bps   = (best_ask - best_bid) / mid * 1e4            half_spread_bps = spread_bps / 2
    BUY  N quote: walk the asks upward, spending N quote          -> VWAP = N / base bought
    SELL N quote: walk the bids downward, receiving N quote       -> VWAP = N / base sold
    slippage_vs_mid_bps     = |VWAP - mid| / mid * 1e4          (half spread + walking the book)
    impact_beyond_touch_bps = |VWAP - touch| / mid * 1e4        (touch = best ask / best bid)

so slippage_vs_mid = half_spread + impact_beyond_touch exactly, per snapshot. When N exceeds the
quote value of the visible levels (10 by default) the walk is undefined: the snapshot is counted as
"insufficient depth" and ranks as +infinity in the quantiles, so a quantile that lands on it is
None (reported as insufficient depth) instead of an optimistic number.

Quantiles are nearest-rank, upper (q-th = the ceil(q*n)-th smallest value): deterministic, no
interpolation, never below the empirical quantile. Values are Decimal, rounded UP to 0.01 bps and
written as strings. Snapshots are sorted by time and de-duplicated on (symbol, ts), so the result
does not depend on file order; the output carries each input file's sha256 and no wall-clock time.

The sample is what the recorder captured: one or a few sessions, a few hours, sampled every ~15-35 s.
It is NOT representative of all hours, weekdays, volatility regimes or news events, and the top 10
levels hide anything deeper. Each symbol therefore carries a confidence note (see docs/COSTS.md).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

CALIBRATION_VERSION = "cal-1"
DEFAULT_NOTIONALS = (250, 500, 1000, 2500)
QUANTILES = (("median", Fraction(1, 2)), ("p90", Fraction(9, 10)))
MIN_SAMPLES = 20          # fewer valid snapshots than this -> symbol not usable (proxy fallback)
GAP_S = 120               # consecutive snapshots further apart than this start a new session
_BPS = Decimal(10000)
_OUT_Q = Decimal("0.01")


@dataclass(frozen=True)
class CostCalibration:
    """The calibration document (JSON-able dict, see `calibrate_costs`)."""
    data: dict

    def to_json(self) -> str:
        return json.dumps(self.data, indent=2, sort_keys=True) + "\n"

    def save(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())
        return p

    @classmethod
    def load(cls, path: Path | str) -> "CostCalibration":
        data = json.loads(Path(path).read_text())
        if data.get("version") != CALIBRATION_VERSION:
            raise ValueError(f"unsupported calibration version {data.get('version')!r}")
        return cls(data)

    @property
    def sha256(self) -> str:
        return self.data["calibration_sha256"]

    def usable(self, symbol: str) -> bool:
        entry = self.data["symbols"].get(symbol)
        return bool(entry and entry.get("usable"))


# ------------------------------------------------------------------------------------------ reading
def read_depth_file(path: Path | str) -> tuple[list[dict], dict]:
    """Rows of one depth file and a summary {file, sha256, lines, rows, bad_lines, torn_last_line}.
    A partially written last line (no newline / not JSON) is skipped and flagged, never fatal."""
    p = Path(path)
    raw = p.read_bytes()
    lines = raw.split(b"\n")
    torn = False
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    elif lines:
        torn = True          # no trailing newline: the writer may be mid-line
    rows, bad = [], 0
    for k, line in enumerate(lines):
        try:
            r = json.loads(line)
            if not isinstance(r, dict):
                raise ValueError("not an object")
        except ValueError:
            if k == len(lines) - 1:
                torn = True
            else:
                bad += 1
            continue
        if torn and k == len(lines) - 1:
            continue         # parsed, but may still be truncated mid-number: do not trust it
        rows.append(r)
    return rows, {"file": p.name, "sha256": hashlib.sha256(raw).hexdigest(), "lines": len(lines),
                  "rows": len(rows), "bad_lines": bad, "torn_last_line": torn}


def _levels(side: Any) -> list[tuple[Decimal, Decimal]] | None:
    try:
        out = [(Decimal(str(p)), Decimal(str(q))) for p, q in side]
    except (TypeError, ValueError, InvalidOperation):
        return None
    if any(not (p.is_finite() and q.is_finite()) for p, q in out):
        return None
    return [(p, q) for p, q in out if p > 0 and q > 0]


# ------------------------------------------------------------------------------------------ maths
def walk_book(levels: Sequence[tuple[Decimal, Decimal]], notional: Decimal) -> Decimal | None:
    """VWAP of spending/receiving `notional` quote through `levels` (best first); None if the
    visible levels hold less than `notional`."""
    remaining, qty = notional, Decimal(0)
    for p, q in levels:
        value = p * q
        if value >= remaining:
            qty += remaining / p
            remaining = Decimal(0)
            break
        qty += q
        remaining -= value
    if remaining > 0 or qty <= 0:
        return None
    return notional / qty


def snapshot_costs(bids: Sequence[tuple[Decimal, Decimal]], asks: Sequence[tuple[Decimal, Decimal]],
                   notionals: Sequence[Decimal]) -> dict | None:
    """Per-snapshot spread, visible depth and, per side and notional, (slippage vs mid, impact beyond
    touch) in bps of mid; None for a crossed/empty book. Values are exact-ish Decimals (50 digits)."""
    bids = sorted(bids, key=lambda x: -x[0])
    asks = sorted(asks, key=lambda x: x[0])
    if not bids or not asks or asks[0][0] <= bids[0][0]:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        bid, ask = bids[0][0], asks[0][0]
        mid = (bid + ask) / 2
        out = {"mid": mid, "spread_bps": (ask - bid) / mid * _BPS,
               "depth_quote": {"BUY": sum((p * q for p, q in asks), Decimal(0)),
                               "SELL": sum((p * q for p, q in bids), Decimal(0))},
               "BUY": {}, "SELL": {}}
        for n in notionals:
            vb = walk_book(asks, n)
            out["BUY"][n] = None if vb is None else ((vb - mid) / mid * _BPS, (vb - ask) / mid * _BPS)
            vs = walk_book(bids, n)
            out["SELL"][n] = None if vs is None else ((mid - vs) / mid * _BPS, (bid - vs) / mid * _BPS)
    return out


def nearest_rank(values: Iterable[Decimal | None], q: Fraction) -> Decimal | None:
    """Upper nearest-rank quantile; None entries rank as +infinity (a quantile landing on one is None)."""
    xs = sorted(values, key=lambda v: (v is None, v if v is not None else 0))
    if not xs:
        return None
    k = max(1, math.ceil(q * len(xs)))
    return xs[k - 1]


def _fmt(v: Decimal | None) -> str | None:
    return None if v is None else str(v.quantize(_OUT_Q, rounding=ROUND_CEILING))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quantiles(values: Sequence[Decimal | None]) -> dict:
    return {name: _fmt(nearest_rank(values, q)) for name, q in QUANTILES}


# ------------------------------------------------------------------------------------------ main entry
def calibrate_costs(depth_files: Sequence[Path | str], symbols: Sequence[str],
                    notionals: Sequence[int | str | Decimal] = DEFAULT_NOTIONALS) -> CostCalibration:
    """Calibrate spread and VWAP slippage per symbol from recorded depth files (see module docstring).
    Deterministic: same file bytes -> byte-identical JSON."""
    ns = sorted({Decimal(str(n)) for n in notionals})
    if not ns or ns[0] <= 0:
        raise ValueError("notionals must be positive")
    keys = [str(int(n)) if n == n.to_integral_value() else str(n) for n in ns]
    files = sorted((Path(f) for f in depth_files), key=lambda p: p.name)
    inputs, rows = [], []
    for f in files:
        rs, info = read_depth_file(f)
        inputs.append(info)
        rows.extend(rs)
    wanted = set(symbols)
    by_symbol: dict[str, dict[float, dict]] = {s: {} for s in sorted(wanted)}
    invalid: dict[str, int] = {s: 0 for s in by_symbol}
    for r in sorted((r for r in rows if r.get("symbol") in wanted),
                    key=lambda r: (str(r["symbol"]), float(r.get("ts") or 0))):
        s = r["symbol"]
        try:
            ts = float(r["ts"])
        except (KeyError, TypeError, ValueError):
            invalid[s] += 1
            continue
        if ts in by_symbol[s]:
            continue                                          # duplicate snapshot
        bids, asks = _levels(r.get("bids") or []), _levels(r.get("asks") or [])
        snap = None if bids is None or asks is None else snapshot_costs(bids, asks, ns)
        if snap is None:
            invalid[s] += 1
            continue
        by_symbol[s][ts] = snap

    out_symbols = {}
    for s, snaps in by_symbol.items():
        out_symbols[s] = _symbol_summary(s, snaps, invalid[s], ns, keys)
    data = {
        "version": CALIBRATION_VERSION,
        "inputs": inputs,
        "notionals_quote": keys,
        "symbols": out_symbols,
        "method": {
            "spread": "(best_ask - best_bid) / mid * 1e4 per snapshot; half spread = spread / 2",
            "slippage_vs_mid": "VWAP of a marketable order of N quote walking the recorded levels, vs mid, bps",
            "impact_beyond_touch": "VWAP vs best ask (BUY) / best bid (SELL), in bps of mid; "
                                   "slippage_vs_mid = half_spread + impact_beyond_touch per snapshot",
            "quantiles": "upper nearest-rank over valid snapshots (each snapshot weighs the same); snapshots "
                         "whose visible depth is below N rank as +inf, so such a quantile is null",
            "rounding": "Decimal, rounded UP to 0.01 bps",
            "min_samples_for_usable": MIN_SAMPLES,
        },
        "confidence_note": (
            "LOW CONFIDENCE. These numbers describe only the recorded snapshots: a few hours of one or two "
            "sessions, top 10 levels, ~15-35 s sampling. Spreads and depth vary by hour of day, weekday, "
            "volatility regime and news; a quiet-hours p90 can be far below a volatile-hours median. Hidden "
            "or deeper liquidity, queue dynamics, latency and our own market impact on subsequent snapshots "
            "are not captured. Refresh after several days covering all UTC hours before relying on them."),
    }
    body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    data["calibration_sha256"] = hashlib.sha256(body).hexdigest()
    return CostCalibration(data)


def _symbol_summary(symbol: str, snaps: Mapping[float, dict], invalid: int, ns: Sequence[Decimal],
                    keys: Sequence[str]) -> dict:
    ts = sorted(snaps)
    n = len(ts)
    entry: dict[str, Any] = {"samples": n, "invalid_snapshots": invalid}
    if n == 0:
        entry.update(usable=False, reason="no valid snapshots", confidence="none")
        return entry
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    covered = sum(g for g in gaps if g <= GAP_S)
    sessions = 1 + sum(1 for g in gaps if g > GAP_S)
    hours = sorted({datetime.fromtimestamp(t, tz=timezone.utc).hour for t in ts})
    vals = [snaps[t] for t in ts]
    entry.update({
        "first": _iso(ts[0]), "last": _iso(ts[-1]),
        "span_hours": f"{(ts[-1] - ts[0]) / 3600:.2f}",
        "covered_hours": f"{covered / 3600:.2f}",
        "sessions": sessions,
        "median_interval_s": f"{sorted(gaps)[len(gaps) // 2]:.1f}" if gaps else None,
        "utc_hours_covered": hours,
        "spread_bps": {**_quantiles([v["spread_bps"] for v in vals]),
                       "max": _fmt(max(v["spread_bps"] for v in vals))},
        "half_spread_bps": _quantiles([v["spread_bps"] / 2 for v in vals]),
        "visible_depth_quote": {side: {"median": _fmt(nearest_rank([v["depth_quote"][side] for v in vals],
                                                                   Fraction(1, 2))),
                                       "p10": _fmt(nearest_rank([v["depth_quote"][side] for v in vals],
                                                                Fraction(1, 10)))}
                                for side in ("BUY", "SELL")},
        "slippage_vs_mid_bps": {}, "impact_beyond_touch_bps": {},
    })
    for side in ("BUY", "SELL"):
        mid_q, touch_q = {}, {}
        for n_dec, key in zip(ns, keys):
            per = [v[side][n_dec] for v in vals]
            short = sum(1 for x in per if x is None)
            flags = {"insufficient_depth_samples": short,
                     "insufficient_depth": short > 0}
            mid_q[key] = {**_quantiles([None if x is None else x[0] for x in per]), **flags}
            touch_q[key] = {**_quantiles([None if x is None else x[1] for x in per]), **flags}
        entry["slippage_vs_mid_bps"][side] = mid_q
        entry["impact_beyond_touch_bps"][side] = touch_q
    defined = [k for k in keys if all(entry["slippage_vs_mid_bps"][sd][k]["p90"] is not None
                                      for sd in ("BUY", "SELL"))]
    usable = n >= MIN_SAMPLES and bool(defined)
    entry["usable"] = usable
    entry["reason"] = ("ok" if usable else
                       f"only {n} valid snapshots (< {MIN_SAMPLES})" if n < MIN_SAMPLES else
                       "visible depth below the smallest notional at p90 on a side")
    entry["notionals_with_p90"] = defined
    all_hours = len(hours) == 24 and covered >= 7 * 86400
    entry["confidence"] = "medium" if all_hours else "low"
    entry["confidence_note"] = (
        f"{n} snapshots over {covered / 3600:.1f} covered hours in {sessions} session(s), UTC hours "
        f"{_hour_ranges(hours)}; " + ("covers every UTC hour over >= 7 days, still one market regime."
                                      if all_hours else
                                      "NOT representative of other hours, days or volatility regimes."))
    return entry


def _hour_ranges(hours: Sequence[int]) -> str:
    if not hours:
        return "-"
    out, start, prev = [], hours[0], hours[0]
    for h in list(hours[1:]) + [None]:
        if h is not None and h == prev + 1:
            prev = h
            continue
        out.append(f"{start:02d}" if start == prev else f"{start:02d}-{prev:02d}")
        if h is not None:
            start = prev = h
    return ",".join(out)


# ------------------------------------------------------------------------------------------ report
def render_calibration_markdown(cal: CostCalibration, headline_notional: str = "1000") -> str:
    d = cal.data
    L = ["# Cost calibration from recorded order books", "",
         f"**{d['confidence_note']}**", "",
         f"- calibration sha256: `{d['calibration_sha256']}` (version {d['version']})",
         "- inputs (copied snapshot of the live recorder files; sha256 of the bytes read):"]
    L += [f"  - `{i['file']}` sha256 `{i['sha256']}`: {i['rows']} rows, {i['bad_lines']} bad lines, "
          f"torn last line: {i['torn_last_line']}" for i in d["inputs"]]
    L += ["", f"Method: {d['method']['slippage_vs_mid']}; {d['method']['impact_beyond_touch']}. "
          f"Quantiles: {d['method']['quantiles']}.", "",
          f"## Headline (order notional {headline_notional} USDT)", "",
          "| symbol | usable | samples | covered h | UTC hours | spread median / p90 (bps) | "
          "BUY slip vs mid median / p90 | SELL slip vs mid median / p90 | visible depth ask / bid median (USDT) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for s, e in sorted(d["symbols"].items()):
        if not e.get("samples"):
            L.append(f"| {s} | no | 0 | - | - | - | - | - | - |")
            continue
        b = e["slippage_vs_mid_bps"]["BUY"].get(headline_notional, {})
        sl = e["slippage_vs_mid_bps"]["SELL"].get(headline_notional, {})
        dep = e["visible_depth_quote"]
        L.append(f"| {s} | {'yes' if e['usable'] else 'NO: ' + e['reason']} | {e['samples']} | "
                 f"{e['covered_hours']} | {_hour_ranges(e['utc_hours_covered'])} | "
                 f"{e['spread_bps']['median']} / {e['spread_bps']['p90']} | "
                 f"{_na(b.get('median'))} / {_na(b.get('p90'))} | {_na(sl.get('median'))} / {_na(sl.get('p90'))} | "
                 f"{dep['BUY']['median']} / {dep['SELL']['median']} |")
    L += ["", "## Slippage vs mid by notional (bps; median / p90; 'insufficient' = visible 10 levels too thin)", "",
          "| symbol | side | " + " | ".join(d["notionals_quote"]) + " |",
          "|---|---|" + "---|" * len(d["notionals_quote"])]
    for s, e in sorted(d["symbols"].items()):
        if not e.get("samples"):
            continue
        for side in ("BUY", "SELL"):
            cells = []
            for k in d["notionals_quote"]:
                c = e["slippage_vs_mid_bps"][side][k]
                tag = f" ({c['insufficient_depth_samples']}/{e['samples']} short)" if c["insufficient_depth"] else ""
                cells.append(f"{_na(c['median'])} / {_na(c['p90'])}{tag}")
            L.append(f"| {s} | {side} | " + " | ".join(cells) + " |")
    L += ["", "## Per-symbol confidence", ""]
    L += [f"- {s}: {e.get('confidence_note', e.get('reason'))}" for s, e in sorted(d["symbols"].items())]
    L.append("")
    return "\n".join(L)


def _na(v: Any) -> str:
    return "insufficient" if v is None else str(v)
