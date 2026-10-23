"""Binance PUBLIC klines (unauthenticated GET only, host pinned to api.binance.com).

Research data only: gold/data/ is filled from here once and committed, so the gold backtest runs
offline. This module is outside every order path and imports nothing from it. Klines are
`[open_time_ms, open, high, low, close, volume, close_time_ms, ...]`; prices arrive as strings
and are parsed to Decimal. The still-forming bar (open + interval > now) is dropped.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from wallexbot.data.candles import Candle
from wallexbot.types import D, canonical_decimal

HOST = "api.binance.com"
KLINES_URL = f"https://{HOST}/api/v3/klines"
INTERVAL_S = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
MAX_LIMIT = 1000

Transport = Callable[[str, float], tuple[int, bytes]]


class BinanceDataError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def urllib_transport(url: str, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "wallexbot-gold-research"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check_url(url: str) -> None:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.hostname != HOST or p.username or p.port not in (None, 443):
        raise BinanceDataError(f"refusing non-pinned url: {p.scheme}://{p.netloc}")


def _num(text) -> str:
    return canonical_decimal(D(text))


def parse_klines(rows, interval_s: int) -> list[Candle]:
    if not isinstance(rows, list):
        raise BinanceDataError(f"klines: expected a list, got {type(rows).__name__}")
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 6:
            raise BinanceDataError(f"klines: malformed row {r!r}")
        ts_ms = int(r[0])
        if ts_ms % 1000 or (ts_ms // 1000) % interval_s:
            raise BinanceDataError(f"klines: open time {ts_ms} not on the {interval_s}s grid")
        out.append(Candle(ts_ms // 1000, D(_num(r[1])), D(_num(r[2])), D(_num(r[3])), D(_num(r[4])),
                          D(_num(r[5]))))
    return out


def fetch_klines(symbol: str, interval: str, start_ts: int, end_ts: int, *,
                 transport: Transport | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, timeout_s: float = 15.0,
                 max_requests: int = 200) -> list[Candle]:
    """CLOSED klines with open time in [start_ts, end_ts], oldest first, paginated by startTime."""
    if interval not in INTERVAL_S:
        raise ValueError(f"unsupported interval {interval!r}")
    if not symbol.isalnum():
        raise ValueError(f"bad symbol {symbol!r}")
    step = INTERVAL_S[interval]
    send = transport or urllib_transport
    now = clock()
    out: dict[int, Candle] = {}
    cursor = int(start_ts)
    for _ in range(max_requests):
        if cursor > end_ts:
            break
        url = KLINES_URL + "?" + urllib.parse.urlencode(
            {"symbol": symbol, "interval": interval, "startTime": cursor * 1000,
             "endTime": int(end_ts) * 1000 + step * 1000 - 1, "limit": MAX_LIMIT})
        check_url(url)
        status, body = send(url, timeout_s)
        if status != 200:
            raise BinanceDataError(f"HTTP {status} from {HOST}: {body[:200]!r}")
        batch = parse_klines(json.loads(body), step)
        fresh = [c for c in batch if c.ts >= cursor]
        if not fresh:
            break
        for c in fresh:
            if c.ts + step <= now and start_ts <= c.ts <= end_ts:
                out[c.ts] = c
        cursor = fresh[-1].ts + step
        sleep(0.2)
    else:
        raise BinanceDataError(f"request budget ({max_requests}) exhausted before {end_ts}")
    return [out[t] for t in sorted(out)]
