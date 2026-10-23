"""Gold data: download once (public GETs only), commit under gold/data/, read back offline.

Files (candle CSVs use the repo format with a `.sha256` sidecar, data/candles.py):
- binance_PAXGUSDT_1h.csv   decision bars for backtest and shadow (Binance, 2026-02-01 -> fetch time)
- binance_PAXGUSDT_5m.csv   finer quotes for the 30-day shadow replay (last ~31 days)
- binance_PAXGUSDT_15m.csv  quotes for the full-window shadow replay (evaluation window, 2026-04-01 ->)
- wallex_PAXGUSDT_1h.csv, wallex_XAUTUSDT_1h.csv   Wallex native 1h candles (repo public client)
- wallex_markets.json       Wallex market rules for the gold tokens (precisions, 24h volume)
- wallex_depth.jsonl        a few Wallex order-book snapshots of the gold markets (top 20 levels)
- fetch_meta.json           what was fetched, when, and the request ranges
"""
from __future__ import annotations

import calendar
import json
import statistics
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from wallexbot.data.candles import Candle, read_candles_csv, write_candles_csv
from wallexbot.gold import binance
from wallexbot.types import SymbolRules, canonical_decimal

DEFAULT_DATA_DIR = Path("gold") / "data"
DATA_START_TS = calendar.timegm((2026, 2, 1, 0, 0, 0))
SHADOW_QUOTE_DAYS = 31
GOLD_MARKETS = ("PAXGUSDT", "XAUTUSDT")
BINANCE_SYMBOL = "PAXGUSDT"
H1, M5 = 3600, 300
DEPTH_LEVELS = 20

F_BINANCE_1H = "binance_PAXGUSDT_1h.csv"
F_BINANCE_5M = "binance_PAXGUSDT_5m.csv"
F_BINANCE_15M = "binance_PAXGUSDT_15m.csv"
QUOTES_15M_START_TS = calendar.timegm((2026, 4, 1, 0, 0, 0))
M15 = 900
F_MARKETS = "wallex_markets.json"
F_DEPTH = "wallex_depth.jsonl"
F_META = "fetch_meta.json"


def wallex_file(symbol: str) -> str:
    return f"wallex_{symbol}_1h.csv"


# ------------------------------------------------------------------ fetch (network, run once)
def fetch_all(data_dir: Path | str = DEFAULT_DATA_DIR, *, wallex_client: Any = None,
              binance_transport: binance.Transport | None = None, clock: Callable[[], float] = time.time,
              sleep: Callable[[float], None] = time.sleep, depth_snapshots: int = 5,
              depth_interval_s: float = 10.0, log: Callable[[str], None] = print) -> dict:
    """Download every gold input. Public GETs only: Binance klines (host-pinned here) and the repo's
    host-pinned Wallex public client. Overwrites the files in `data_dir`."""
    from wallexbot.exchange.wallex_public import WallexPublicClient
    out = Path(data_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = clock()
    end_1h = int(now) // H1 * H1 - H1                     # open time of the last closed hour
    h1 = binance.fetch_klines(BINANCE_SYMBOL, "1h", DATA_START_TS, end_1h, transport=binance_transport,
                              clock=clock, sleep=sleep)
    write_candles_csv(out / F_BINANCE_1H, h1)
    log(f"binance {BINANCE_SYMBOL} 1h: {len(h1)} bars")
    m5_start = (end_1h + H1) // 86400 * 86400 - SHADOW_QUOTE_DAYS * 86400
    m5_end = end_1h + H1 - M5                             # 5m bars inside the last closed hour
    m5 = binance.fetch_klines(BINANCE_SYMBOL, "5m", m5_start, m5_end, transport=binance_transport,
                              clock=clock, sleep=sleep)
    write_candles_csv(out / F_BINANCE_5M, m5)
    log(f"binance {BINANCE_SYMBOL} 5m: {len(m5)} bars")
    m15 = fetch_quotes_15m(out, end_1h, transport=binance_transport, clock=clock, sleep=sleep)
    log(f"binance {BINANCE_SYMBOL} 15m: {len(m15)} bars")

    client = wallex_client or WallexPublicClient(clock=clock, sleep=sleep)
    wallex_counts = {}
    for sym in GOLD_MARKETS:
        cs = client.candles(sym, "60", DATA_START_TS, end_1h)
        write_candles_csv(out / wallex_file(sym), cs)
        wallex_counts[sym] = len(cs)
        log(f"wallex {sym} 1h: {len(cs)} bars")
    markets = {}
    for m in client._spot_markets():
        if m.get("symbol") in GOLD_MARKETS:
            markets[m["symbol"]] = {k: (canonical_decimal(m[k]) if isinstance(m.get(k), Decimal) else m.get(k))
                                    for k in ("symbol", "base_asset", "quote_asset", "amount_precision",
                                              "price_precision", "quote_volume_24h", "volume_24h")}
    (out / F_MARKETS).write_text(json.dumps({"fetched_at": int(now), "markets": markets}, indent=1, sort_keys=True)
                                 + "\n", encoding="utf-8")
    with (out / F_DEPTH).open("w", encoding="utf-8", newline="\n") as fh:
        for k in range(depth_snapshots):
            for sym in GOLD_MARKETS:
                d = client.depth(sym)
                rec = {"symbol": sym, "ts": round(float(d.ts), 3),
                       "asks": [[canonical_decimal(p), canonical_decimal(q)] for p, q in d.asks[:DEPTH_LEVELS]],
                       "bids": [[canonical_decimal(p), canonical_decimal(q)] for p, q in d.bids[:DEPTH_LEVELS]]}
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
            if k + 1 < depth_snapshots:
                sleep(depth_interval_s)
    meta = {"fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "binance": {"symbol": BINANCE_SYMBOL, "1h": [DATA_START_TS, end_1h, len(h1)],
                        "5m": [m5_start, m5_end, len(m5)],
                        "15m": [QUOTES_15M_START_TS, end_1h + H1 - M15, len(m15)], "source": binance.KLINES_URL},
            "wallex": {"1h_bars": wallex_counts, "depth_snapshots": depth_snapshots,
                       "source": "api.wallex.ir /v1/udf/history (resolution 60), /v1/depth, markets"}}
    (out / F_META).write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return meta


def fetch_quotes_15m(data_dir: Path | str, end_1h: int, *, transport: binance.Transport | None = None,
                     clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> list[Candle]:
    """Binance 15m klines from the evaluation start to the end of the hour whose open is `end_1h`."""
    m15 = binance.fetch_klines(BINANCE_SYMBOL, "15m", QUOTES_15M_START_TS, end_1h + H1 - M15, transport=transport,
                               clock=clock, sleep=sleep)
    write_candles_csv(Path(data_dir) / F_BINANCE_15M, m15)
    return m15


# ------------------------------------------------------------------ read back (offline)
@dataclass(frozen=True)
class GoldData:
    h1: tuple[Candle, ...]                       # Binance PAXGUSDT 1h
    m5: tuple[Candle, ...]                       # Binance PAXGUSDT 5m (30-day shadow window)
    m15: tuple[Candle, ...]                      # Binance PAXGUSDT 15m (full-window shadow)
    wallex_h1: Mapping[str, tuple[Candle, ...]]  # Wallex native 1h per gold market
    markets: Mapping[str, Mapping[str, Any]]
    depth: tuple[Mapping[str, Any], ...]
    meta: Mapping[str, Any]


def load(data_dir: Path | str = DEFAULT_DATA_DIR) -> GoldData:
    d = Path(data_dir)
    if not (d / F_BINANCE_1H).is_file():
        raise FileNotFoundError(f"{d / F_BINANCE_1H} not found; run `python -m wallexbot gold-fetch` first")
    wallex = {s: tuple(read_candles_csv(d / wallex_file(s))) for s in GOLD_MARKETS if (d / wallex_file(s)).is_file()}
    depth = tuple(json.loads(line) for line in (d / F_DEPTH).read_text(encoding="utf-8").splitlines()
                  if line.strip()) if (d / F_DEPTH).is_file() else ()
    markets = json.loads((d / F_MARKETS).read_text(encoding="utf-8"))["markets"] if (d / F_MARKETS).is_file() else {}
    meta = json.loads((d / F_META).read_text(encoding="utf-8")) if (d / F_META).is_file() else {}
    m5 = tuple(read_candles_csv(d / F_BINANCE_5M)) if (d / F_BINANCE_5M).is_file() else ()
    m15 = tuple(read_candles_csv(d / F_BINANCE_15M)) if (d / F_BINANCE_15M).is_file() else ()
    return GoldData(tuple(read_candles_csv(d / F_BINANCE_1H)), m5, m15, wallex, markets, depth, meta)


def symbol_rules(data: GoldData, symbol: str = "PAXGUSDT") -> SymbolRules:
    """Wallex rules for the traded market (precisions from the markets endpoint; min notional is
    unverified on Wallex, the risk config's 5 USDT applies)."""
    m = data.markets.get(symbol)
    if m is None:   # offline fallback = the values observed on 2026-10-23
        return SymbolRules(symbol, symbol[:-4], "USDT", amount_precision=6, price_precision=2)
    return SymbolRules(symbol, m["base_asset"], m["quote_asset"], amount_precision=int(m["amount_precision"]),
                       price_precision=int(m["price_precision"]))


# ------------------------------------------------------------------ Binance vs Wallex basis
def _pct(xs: Sequence[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def basis_report(binance_h1: Sequence[Candle], wallex_h1: Sequence[Candle]) -> dict:
    """Wallex close vs Binance PAXG close on common hours, in bps of the Binance close (positive =
    Wallex above). Hours with zero Wallex volume (forward-filled, no trade) are reported separately:
    a flat no-trade bar carries a stale price."""
    b = {c.ts: c for c in binance_h1}
    traded, all_ = [], []
    for w in wallex_h1:
        c = b.get(w.ts)
        if c is None:
            continue
        bps = float((w.close - c.close) / c.close * 10000)
        all_.append(bps)
        if w.volume > 0:
            traded.append(bps)

    def stats(xs):
        return {"n": len(xs), "median_bps": statistics.median(xs) if xs else None,
                "mean_bps": statistics.fmean(xs) if xs else None,
                "p05_bps": _pct(xs, 0.05), "p95_bps": _pct(xs, 0.95),
                "mean_abs_bps": statistics.fmean(abs(x) for x in xs) if xs else None}

    vols = [float(w.volume * w.close) for w in wallex_h1]
    return {"common_hours": len(all_), "traded_hours": stats(traded), "all_hours": stats(all_),
            "zero_volume_hour_share": (sum(1 for w in wallex_h1 if w.volume == 0) / len(wallex_h1)) if wallex_h1 else None,
            "median_hourly_notional_usdt": statistics.median(vols) if vols else None,
            "first_ts": wallex_h1[0].ts if wallex_h1 else None, "last_ts": wallex_h1[-1].ts if wallex_h1 else None}


def depth_report(snapshots: Sequence[Mapping[str, Any]], notionals: Sequence[int] = (250, 1000)) -> dict:
    """Per market: median half-spread (bps of mid) and the VWAP cost of a market BUY / SELL of each
    notional against the visible levels (bps vs mid; None if the visible depth is too thin)."""
    out: dict[str, Any] = {}
    for sym in sorted({s["symbol"] for s in snapshots}):
        rows = [s for s in snapshots if s["symbol"] == sym]
        half, walk = [], {n: {"BUY": [], "SELL": []} for n in notionals}
        for s in rows:
            asks = [(Decimal(p), Decimal(q)) for p, q in s["asks"]]
            bids = [(Decimal(p), Decimal(q)) for p, q in s["bids"]]
            mid = (asks[0][0] + bids[0][0]) / 2
            half.append(float((asks[0][0] - bids[0][0]) / 2 / mid * 10000))
            for n in notionals:
                for side, levels in (("BUY", asks), ("SELL", bids)):
                    left, qty, quote = Decimal(n), Decimal(0), Decimal(0)
                    for p, q in levels:
                        take = min(q, left / p)
                        qty += take
                        quote += take * p
                        left -= take * p
                        if left <= Decimal("1e-9"):
                            break
                    if left > Decimal("1e-9"):
                        walk[n][side].append(None)
                        continue
                    vwap = quote / qty
                    walk[n][side].append(float(abs(vwap - mid) / mid * 10000))
        out[sym] = {"snapshots": len(rows), "median_half_spread_bps": statistics.median(half),
                    "max_half_spread_bps": max(half),
                    "vwap_cost_vs_mid_bps": {str(n): {side: (statistics.median([x for x in v if x is not None])
                                                             if any(x is not None for x in v) else None)
                                                      for side, v in walk[n].items()} for n in notionals},
                    "best_ask_qty_usdt": float(Decimal(rows[-1]["asks"][0][0]) * Decimal(rows[-1]["asks"][0][1])),
                    "best_bid_qty_usdt": float(Decimal(rows[-1]["bids"][0][0]) * Decimal(rows[-1]["bids"][0][1]))}
    return out
