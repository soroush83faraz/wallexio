"""Wallex PUBLIC market data (unauthenticated GET only). Host pinned; parsers fail closed.

Verified against the live API (see tests/fixtures):
- /hector/web/v1/markets, /v1/depth, /v1/trades wrap payloads as {"result": ..., "success": true}.
- /v1/udf/history returns TradingView UDF arrays t,o,h,l,c,v; t is the bar OPEN time (unix s) and the
  newest bar is still forming. Only resolutions 1, 60 and 1D are really honoured: 15 returns 1m bars,
  240/480/720 return 1h bars, 2D/3D return 1D bars. We validate the returned grid and raise instead
  of silently mislabelling. Ranges are capped server side (1m over 2 days -> "no_data"; 60 over 90
  days is silently truncated to the latest ~696 bars), so candles() chunks at <= 600 bars.
- The 1m series forward-fills no-trade minutes as flat zero-volume bars (so the grid is dense and zero-volume
  runs are normal); native hourly open = open of the first bar that actually traded.
- Trades carry no id; a trade is identified by (timestamp, price, quantity, side).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from wallexbot.config import ALLOWED_HOSTS
from wallexbot.data.candles import Candle, CandleDataError
from wallexbot.types import D, Quote, SymbolRules

HOST = "api.wallex.ir"
BASE_URL = f"https://{HOST}"
RESOLUTION_SECONDS = {"1": 60, "15": 900, "60": 3600, "240": 14400, "480": 28800, "720": 43200,
                      "1D": 86400, "2D": 172800, "3D": 259200}
MAX_BARS_PER_REQUEST = 600
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")

# transport(method, url, timeout) -> (http_status, body_bytes); raises OSError/TimeoutError on network failure
Transport = Callable[[str, str, float], "tuple[int, bytes]"]


class WallexDataError(ValueError):
    """Response missing/invalid required fields. Callers must treat the data as unusable."""


class WallexHTTPError(RuntimeError):
    """Request failed (non-retryable status, or retries exhausted)."""


class WallexHostError(ValueError):
    """URL is not https://api.wallex.ir or method is not GET."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def urllib_transport(method: str, url: str, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method=method, headers={"Accept": "application/json", "User-Agent": "wallexbot/0.1"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:  # non-2xx: report status, let the client decide
        return e.code, e.read()


@dataclass(frozen=True)
class Trade:
    symbol: str
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    is_buy: bool  # taker side as reported by the exchange (isBuyOrder)
    ts: float  # unix seconds UTC

    @property
    def key(self) -> tuple:
        """No trade id exists on Wallex; this is the dedupe identity."""
        return (self.ts, str(self.price), str(self.quantity), self.is_buy)


@dataclass(frozen=True)
class Depth:
    symbol: str
    asks: tuple[tuple[Decimal, Decimal], ...]  # (price, quantity), best (lowest) first
    bids: tuple[tuple[Decimal, Decimal], ...]  # (price, quantity), best (highest) first
    ts: float  # local receive time, unix seconds

    @property
    def quote(self) -> Quote:
        return Quote(self.symbol, bid=self.bids[0][0], ask=self.asks[0][0], ts=self.ts)


def _num(value, what: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float, Decimal)):
        raise WallexDataError(f"{what}: not numeric: {value!r}")
    try:
        d = D(value)
    except (ValueError, ArithmeticError) as e:
        raise WallexDataError(f"{what}: not numeric: {value!r}") from e
    if d < 0 or (positive and d == 0):
        raise WallexDataError(f"{what}: out of range: {value!r}")
    return d


def _req(obj, key: str, what: str):
    if not isinstance(obj, dict) or key not in obj or obj[key] is None:
        raise WallexDataError(f"{what}: missing required field {key!r}")
    return obj[key]


def _int(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WallexDataError(f"{what}: not a non-negative integer: {value!r}")
    return value


def _check_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol):
        raise ValueError(f"invalid symbol {symbol!r}")
    return symbol


def _envelope(payload, what: str):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise WallexDataError(f"{what}: response not successful")
    return _req(payload, "result", what)


class WallexPublicClient:
    def __init__(self, transport: Transport | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, timeout_s: float = 10.0, max_retries: int = 3,
                 backoff_s: float = 0.5, min_interval_s: float = 0.25):
        self._transport = transport or urllib_transport
        self._clock = clock
        self._sleep = sleep
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._backoff = backoff_s
        self._min_interval = min_interval_s
        self._last_request = None  # type: float | None

    # ---- transport layer -------------------------------------------------
    def _check_url(self, url: str) -> None:
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.hostname != HOST or p.username or p.port not in (None, 443):
            raise WallexHostError(f"refusing non-pinned url: {p.scheme}://{p.netloc}")

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _request(self, method: str, url: str):
        """Only GET exists. Retries (bounded) on 5xx, 429 and network errors/timeouts."""
        if method != "GET":
            raise WallexHostError(f"only GET is permitted, got {method!r}")
        self._check_url(url)
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                self._sleep(self._backoff * 2 ** (attempt - 1))
            self._throttle()
            try:
                status, body = self._transport(method, url, self._timeout)
            except (OSError, TimeoutError) as e:  # URLError and socket.timeout are OSErrors
                last = e
                continue
            if status == 429 or status >= 500:
                last = WallexHTTPError(f"HTTP {status}")
                if status == 429:
                    self._sleep(self._backoff * 2 ** attempt)  # extra cool-off on rate limiting
                continue
            if status != 200:
                raise WallexHTTPError(f"HTTP {status} for {url}")
            try:
                # parse_float=Decimal keeps JSON numbers (e.g. depth prices) exact
                return json.loads(body, parse_float=Decimal)
            except ValueError as e:
                raise WallexDataError(f"invalid JSON from {url}") from e
        raise WallexHTTPError(f"GET failed after {self._max_retries + 1} attempts: {last}")

    def get_json(self, path_or_url: str, params: dict | None = None):
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._request("GET", url)

    # ---- markets ---------------------------------------------------------
    def _spot_markets(self) -> list[dict]:
        result = _envelope(self.get_json("/hector/web/v1/markets"), "markets")
        markets = _req(result, "markets", "markets")
        if not isinstance(markets, list) or not markets:
            raise WallexDataError("markets: empty or malformed list")
        return [m for m in markets if _req(m, "is_spot", "market") is True]

    @staticmethod
    def _rules(m: dict) -> SymbolRules:
        sym = _req(m, "symbol", "market")
        what = f"market {sym}"
        return SymbolRules(
            symbol=sym, base=_req(m, "base_asset", what), quote=_req(m, "quote_asset", what),
            amount_precision=_int(_req(m, "amount_precision", what), what + " amount_precision"),
            price_precision=_int(_req(m, "price_precision", what), what + " price_precision"),
            min_notional=None,  # otc_min_notional is OTC-only; spot minimum is unverified
        )

    def markets(self) -> list[SymbolRules]:
        return [self._rules(m) for m in self._spot_markets()]

    def rank_universe(self, quote_ccy: str, top_n: int) -> list[SymbolRules]:
        """Spot markets quoted in `quote_ccy`, ranked by 24h quote volume (desc); no volume => skipped."""
        ranked = []
        for m in self._spot_markets():
            if _req(m, "quote_asset", "market") != quote_ccy:
                continue
            vol = m.get("quote_volume_24h")
            if vol is None:
                continue
            ranked.append((_num(vol, f"market {m['symbol']} quote_volume_24h"), self._rules(m)))
        ranked.sort(key=lambda x: (-x[0], x[1].symbol))
        return [r for _, r in ranked[:top_n]]

    # ---- candles ---------------------------------------------------------
    def _parse_candles(self, payload, res_s: int) -> list[Candle]:
        status = _req(payload, "s", "udf")
        if status == "no_data":
            return []
        if status != "ok":
            raise WallexDataError(f"udf: status {status!r}: {payload.get('errmsg')!r}")
        cols = {k: _req(payload, k, "udf") for k in "tohlcv"}
        n = len(cols["t"])
        if any(not isinstance(v, list) or len(v) != n for v in cols.values()):
            raise WallexDataError("udf: arrays missing or of unequal length")
        out: list[Candle] = []
        prev = None
        for i in range(n):
            ts = _int(cols["t"][i], "udf t")
            if ts % res_s or (prev is not None and ts - prev < res_s):
                raise WallexDataError(f"udf: bar grid does not match requested resolution ({res_s}s) at t={ts}")
            prev = ts
            what = f"udf bar t={ts}"
            try:
                out.append(Candle(ts, _num(cols["o"][i], what + " o", True), _num(cols["h"][i], what + " h", True),
                                  _num(cols["l"][i], what + " l", True), _num(cols["c"][i], what + " c", True),
                                  _num(cols["v"][i], what + " v")))
            except CandleDataError as e:
                raise WallexDataError(str(e)) from e
        return out

    def candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        """CLOSED candles with open time in [start, end] (unix s), oldest first. The still-forming
        bar (open + resolution > now) is dropped. Long ranges are fetched in chunks."""
        _check_symbol(symbol)
        if resolution not in RESOLUTION_SECONDS:
            raise ValueError(f"unsupported resolution {resolution!r}; use one of {sorted(RESOLUTION_SECONDS)}")
        res_s = RESOLUTION_SECONDS[resolution]
        now = self._clock()
        span = MAX_BARS_PER_REQUEST * res_s
        out: dict[int, Candle] = {}
        lo = int(start)
        while lo <= end:
            hi = min(lo + span - 1, int(end))
            payload = self.get_json("/v1/udf/history", {"symbol": symbol, "resolution": resolution, "from": lo, "to": hi})
            for c in self._parse_candles(payload, res_s):
                if c.ts + res_s <= now and start <= c.ts <= end:
                    out[c.ts] = c
            lo = hi + 1
        return [out[t] for t in sorted(out)]

    # ---- order book / trades ----------------------------------------------
    def depth(self, symbol: str) -> Depth:
        _check_symbol(symbol)
        result = _envelope(self.get_json("/v1/depth", {"symbol": symbol}), "depth")

        def side(name: str) -> list[tuple[Decimal, Decimal]]:
            rows = _req(result, name, "depth")
            if not isinstance(rows, list) or not rows:
                raise WallexDataError(f"depth: empty or malformed {name}")
            return [(_num(_req(r, "price", f"depth {name}"), f"depth {name} price", True),
                     _num(_req(r, "quantity", f"depth {name}"), f"depth {name} quantity", True)) for r in rows]

        asks = tuple(sorted(side("ask"), key=lambda x: x[0]))
        bids = tuple(sorted(side("bid"), key=lambda x: -x[0]))
        if asks[0][0] <= bids[0][0]:
            raise WallexDataError(f"depth: crossed book {bids[0][0]} >= {asks[0][0]}")
        return Depth(symbol, asks, bids, self._clock())

    def trades(self, symbol: str) -> list[Trade]:
        """Latest 50 public trades only (no history endpoint), oldest first."""
        _check_symbol(symbol)
        result = _envelope(self.get_json("/v1/trades", {"symbol": symbol}), "trades")
        rows = _req(result, "latestTrades", "trades")
        if not isinstance(rows, list):
            raise WallexDataError("trades: malformed latestTrades")
        out = []
        for r in rows:
            side = _req(r, "isBuyOrder", "trade")
            if not isinstance(side, bool):
                raise WallexDataError(f"trade: isBuyOrder not boolean: {side!r}")
            try:
                dt = datetime.fromisoformat(_req(r, "timestamp", "trade"))
                if dt.tzinfo is None:
                    raise ValueError("naive timestamp")
                ts = dt.astimezone(timezone.utc).timestamp()
            except (ValueError, TypeError) as e:
                raise WallexDataError(f"trade: bad timestamp {r.get('timestamp')!r}") from e
            out.append(Trade(symbol, _num(_req(r, "price", "trade"), "trade price", True),
                             _num(_req(r, "quantity", "trade"), "trade quantity", True),
                             _num(_req(r, "sum", "trade"), "trade sum"), side, ts))
        out.reverse()  # API is newest-first; stable sort keeps same-second order deterministic
        out.sort(key=lambda t: t.ts)
        return out
