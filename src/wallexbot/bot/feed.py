"""Market data for the trading loop.

`MarketFeed` is everything the bot reads about the market: the trading universe, CLOSED 15m bars,
the latest quote and visible depth, and a `MarketContext` (spread, depth-walk slippage for the
intended order size, taker fee from config) for the strategy's cost gate.

- `PublicFeed` polls `WallexPublicClient` (public GETs only). Depth (and the quote derived from it)
  every `poll_interval_s` (12 s) per watched symbol; 1m candles incrementally (only minutes after
  the newest one held, at most every `candle_poll_s`), aggregated to 15m with `data.aggregate`
  (a bucket is emitted only once the data shows it ended). Requests per refresh are budgeted
  (`max_requests_per_refresh`) on top of the client's own throttle/backoff; a symbol whose request
  fails keeps its old data, which then ages into "stale" (quote.ts is the local receive time). The
  universe is the top-N spot markets in the quote currency by 24h quote volume
  (`client.rank_universe`), refreshed daily and filtered by spread, visible ask depth and
  zero-volume runs.
- `ReplayFeed` drives the same interface from pre-loaded 1m candles and a synthetic book around
  the close of the last CLOSED 1m candle (tests, offline demo). `on_book` mirrors that book into a
  paper broker so fills happen at replay prices.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from wallexbot.config import Config
from wallexbot.data.aggregate import aggregate
from wallexbot.data.candles import Candle
from wallexbot.exchange.wallex_public import MAX_BARS_PER_REQUEST, Depth
from wallexbot.strategy.contract import MarketContext
from wallexbot.types import Quote, SymbolRules, round_down

BAR_S = 900
MIN_S = 60
BAR_MINUTES = BAR_S // MIN_S
_BPS = Decimal(10000)
_ZERO = Decimal(0)
_Q = Decimal("0.0001")

BookCallback = Callable[[str, list, list], object]   # (symbol, bids, asks), best levels first


class FeedError(RuntimeError):
    """The feed could not refresh anything (every request of this refresh failed)."""


class MarketFeed(Protocol):
    def symbols(self) -> list[str]: ...                       # current trading universe
    def track(self, symbols: Iterable[str]) -> None: ...      # extra symbols to keep fresh (open positions)
    def refresh(self, now: float) -> None: ...
    def closed_bars(self, symbol: str, n: int) -> list[Candle]: ...   # 15m, closed only, oldest first
    def quote(self, symbol: str) -> Quote | None: ...
    def depth(self, symbol: str) -> Depth | None: ...
    def market_context(self, symbol: str, notional: Decimal | None = None) -> MarketContext | None: ...


# ---------------------------------------------------------------------------- helpers
def slippage_bps(levels: Sequence[tuple[Decimal, Decimal]], notional: Decimal | None) -> Decimal | None:
    """Bps by which the VWAP of taking `notional` (quote currency) from `levels` (best first) is
    worse than the best level. None when the visible depth cannot fill it or the input is bad."""
    if notional is None or notional <= 0 or not levels or levels[0][0] <= 0:
        return None
    best = levels[0][0]
    need, cost, qty = notional, _ZERO, _ZERO
    for px, q in levels:
        if px <= 0 or q <= 0:
            continue
        take = min(need, px * q)
        cost += take
        qty += take / px
        need -= take
        if need <= 0:
            break
    if need > 0 or qty <= 0:
        return None
    return abs(cost / qty / best - 1) * _BPS


def notional_within(levels: Sequence[tuple[Decimal, Decimal]], limit: Decimal, *, asks: bool) -> Decimal:
    """Visible notional at levels no worse than `limit` (asks: price <= limit; bids: price >= limit)."""
    return sum((px * q for px, q in levels if (px <= limit if asks else px >= limit)), _ZERO)


def longest_zero_volume_run(candles: Sequence[Candle]) -> int:
    best = run = 0
    for c in candles:
        run = run + 1 if c.volume == 0 else 0
        best = max(best, run)
    return best


def explode_bars(bars: Sequence[Candle], minutes: int = BAR_MINUTES) -> list[Candle]:
    """1m candles that aggregate back exactly to `bars` (tests / demos): price path open -> first
    extreme -> second extreme -> close (an up bar visits its low first), then flat at the close.
    The whole volume sits in the first minute."""
    if minutes < 3:
        raise ValueError("minutes must be >= 3")
    out: list[Candle] = []
    for b in bars:
        first, second = (b.low, b.high) if b.close >= b.open else (b.high, b.low)
        path = [b.open, first, second, b.close] + [b.close] * (minutes - 3)
        for i in range(minutes):
            o, c = path[i], path[i + 1]
            out.append(Candle(b.ts + MIN_S * i, o, max(o, c), min(o, c), c, b.volume if i == 0 else _ZERO))
    return out


# ---------------------------------------------------------------------------- base
class _FeedBase:
    config: Config

    def __init__(self, config: Config) -> None:
        self.config = config
        self.default_notional = config.risk.max_order_value

    def quote(self, symbol: str) -> Quote | None:   # pragma: no cover - overridden
        return None

    def depth(self, symbol: str) -> Depth | None:   # pragma: no cover - overridden
        return None

    def market_context(self, symbol: str, notional: Decimal | None = None) -> MarketContext | None:
        """Live execution costs: quoted spread, depth-walk slippage on the ask side for `notional`
        (default risk.max_order_value) and the configured taker fee. None if unknown / too thin."""
        q, d = self.quote(symbol), self.depth(symbol)
        if q is None or d is None or q.bid <= 0 or q.ask < q.bid:
            return None
        slip = slippage_bps(d.asks, notional if notional is not None else self.default_notional)
        if slip is None:
            return None
        return MarketContext(spread_bps=q.spread_bps.quantize(_Q), est_slippage_bps=slip.quantize(_Q),
                             taker_fee_rate=self.config.taker_rate)


# ---------------------------------------------------------------------------- public (polling)
@dataclass(frozen=True)
class FeedParams:
    poll_interval_s: float = 12.0          # depth / quote per symbol (10-15 s)
    candle_poll_s: float = 60.0            # incremental 1m candle fetch per symbol
    universe_refresh_s: float = 86400.0    # daily
    universe_retry_s: float = 300.0        # after a failed universe refresh
    history_bars: int = 300                # 15m bars kept per symbol (strategy history_bars)
    max_requests_per_refresh: int = 60     # budget for depth + candle requests in one refresh
    universe_oversample: int = 2           # rank top_n * this, keep the first top_n that pass
    zero_volume_lookback_min: int = 360
    max_zero_volume_run_min: int = 60
    universe_depth_notional: Decimal | None = None   # None: min_depth_multiple x risk.max_order_value

    def __post_init__(self) -> None:
        if not 1 <= self.poll_interval_s <= 600 or self.candle_poll_s < MIN_S:
            raise ValueError("poll_interval_s in [1, 600], candle_poll_s >= 60")
        if self.history_bars < 1 or self.max_requests_per_refresh < 1 or self.universe_oversample < 1:
            raise ValueError("history_bars, max_requests_per_refresh, universe_oversample must be >= 1")


class PublicFeed(_FeedBase):
    def __init__(self, client, config: Config, *, params: FeedParams = FeedParams(),
                 on_book: BookCallback | None = None) -> None:
        super().__init__(config)
        self.client = client
        self.params = params
        self.on_book = on_book
        self.universe_report: dict[str, str] = {}
        self.last_errors: list[str] = []
        self._universe: list[str] = []
        self._universe_ts: float | None = None
        self._universe_try: float | None = None
        self._extra: set[str] = set()
        self._m1: dict[str, list[Candle]] = {}
        self._bars: dict[str, tuple[tuple[int, int], list[Candle]]] = {}
        self._depths: dict[str, Depth] = {}
        self._depth_try: dict[str, float] = {}
        self._candle_try: dict[str, float] = {}
        self._spent = 0
        self._rr = -1

    # ---- MarketFeed
    def symbols(self) -> list[str]:
        return list(self._universe)

    def track(self, symbols: Iterable[str]) -> None:
        self._extra = set(symbols)

    def quote(self, symbol: str) -> Quote | None:
        d = self._depths.get(symbol)
        return None if d is None else d.quote

    def depth(self, symbol: str) -> Depth | None:
        return self._depths.get(symbol)

    def closed_bars(self, symbol: str, n: int) -> list[Candle]:
        m1 = self._m1.get(symbol)
        if not m1:
            return []
        key = (len(m1), m1[-1].ts)
        cached = self._bars.get(symbol)
        if cached is None or cached[0] != key:
            cached = (key, aggregate(m1, BAR_MINUTES))   # closed per data only
            self._bars[symbol] = cached
        return cached[1][-n:] if n > 0 else []

    def refresh(self, now: float) -> None:
        p = self.params
        self._reanchor(now)
        self._spent = 0
        self.last_errors = []
        attempted = ok = 0
        due = self._universe_ts is None or now - self._universe_ts >= p.universe_refresh_s
        if due and (self._universe_try is None or now - self._universe_try >= p.universe_retry_s):
            self._universe_try = now
            attempted += 1
            try:
                self._refresh_universe(now)
                ok += 1
            except Exception as e:
                self.last_errors.append(f"universe:{type(e).__name__}")
        # open positions first (their stops depend on it), then the universe round-robin so a
        # tight budget cannot starve the same symbols on every refresh
        rest = sorted(set(self._universe) - self._extra)
        self._rr = (self._rr + 1) % max(1, len(rest))
        for sym in sorted(self._extra) + rest[self._rr:] + rest[:self._rr]:
            if now - self._depth_try.get(sym, -math.inf) >= p.poll_interval_s and self._spend(1):
                self._depth_try[sym] = now
                attempted += 1
                ok += self._poll_depth(sym)
            if now - self._candle_try.get(sym, -math.inf) >= p.candle_poll_s:
                r = self._poll_candles(sym, now)
                attempted += r is not None
                ok += bool(r)
        if attempted and not ok:
            raise FeedError("every market-data request of this refresh failed: " + ",".join(self.last_errors[:5]))

    # ---- internals
    def _reanchor(self, now: float) -> None:
        """Backward clock step: a retry/refresh timestamp after `now` would stall its timer
        (`now - last >= interval` stays false until the clock catches up). Clamp every one to `now`,
        so each timer re-anchors to the new clock and runs one full interval (exchange/ratelimit.py
        clamps its token bucket the same way)."""
        if self._universe_ts is not None and not self._universe_ts <= now:
            self._universe_ts = now
        if self._universe_try is not None and not self._universe_try <= now:
            self._universe_try = now
        for timers in (self._depth_try, self._candle_try):
            for sym, last in list(timers.items()):
                if not last <= now:
                    timers[sym] = now

    def _spend(self, n: int) -> bool:
        if self._spent + n > self.params.max_requests_per_refresh:
            return False
        self._spent += n
        return True

    def _poll_depth(self, sym: str) -> int:
        try:
            d = self.client.depth(sym)
        except Exception as e:
            self.last_errors.append(f"depth:{sym}:{type(e).__name__}")
            return 0
        self._depths[sym] = d
        if self.on_book is not None:
            try:
                self.on_book(sym, list(d.bids), list(d.asks))
            except Exception as e:
                self.last_errors.append(f"on_book:{sym}:{type(e).__name__}")
        return 1

    def _poll_candles(self, sym: str, now: float) -> bool | None:
        """None: nothing to do / no budget (not an attempt); True/False: request outcome."""
        end = int(now) // MIN_S * MIN_S - MIN_S          # open time of the newest CLOSED minute
        held = self._m1.get(sym)
        keep = (self.params.history_bars + 2) * BAR_MINUTES
        start = held[-1].ts + MIN_S if held else end - (keep - 1) * MIN_S
        if start > end:
            self._candle_try[sym] = now
            return None
        chunks = -(-((end - start) // MIN_S + 1) // MAX_BARS_PER_REQUEST)
        if not self._spend(chunks):
            return None
        self._candle_try[sym] = now
        try:
            new = self.client.candles(sym, "1", start, end)
        except Exception as e:
            self.last_errors.append(f"candles:{sym}:{type(e).__name__}")
            return False
        merged = list(held or [])
        for c in new:
            if not merged or c.ts > merged[-1].ts:
                merged.append(c)
        self._m1[sym] = merged[-keep:]
        return True

    def _refresh_universe(self, now: float) -> None:
        """Top-N by 24h quote volume, keeping only markets whose quoted spread <= max_spread_bps,
        whose visible ask notional within max_slippage_bps of the best ask >= the depth requirement,
        and whose longest zero-volume 1m run over the lookback <= max_zero_volume_run_min.
        Universe requests are outside the per-refresh budget (once a day) but go through the
        client's throttle. Raises if ranking fails (the previous universe is kept)."""
        cfg, p = self.config, self.params
        ranked = self.client.rank_universe(cfg.quote_currency, cfg.top_n * p.universe_oversample)
        need = p.universe_depth_notional if p.universe_depth_notional is not None \
            else cfg.min_depth_multiple * cfg.risk.max_order_value
        end = int(now) // MIN_S * MIN_S - MIN_S
        chosen: list[str] = []
        report: dict[str, str] = {}
        for r in ranked:
            if len(chosen) >= cfg.top_n:
                break
            sym = r.symbol
            try:
                d = self.client.depth(sym)
                q = d.quote
                if q.spread_bps > cfg.max_spread_bps:
                    report[sym] = f"spread {q.spread_bps:.2f}bps > {cfg.max_spread_bps}"
                    continue
                band = q.ask * (1 + cfg.max_slippage_bps / _BPS)
                dn = notional_within(d.asks, band, asks=True)
                if dn < need:
                    report[sym] = f"ask depth {dn:.2f} within {cfg.max_slippage_bps}bps < {need}"
                    continue
                m1 = self.client.candles(sym, "1", end - (p.zero_volume_lookback_min - 1) * MIN_S, end)
                run = longest_zero_volume_run(m1)
                if not m1 or run > p.max_zero_volume_run_min:
                    report[sym] = f"zero-volume run {run}min > {p.max_zero_volume_run_min}" if m1 else "no candles"
                    continue
            except Exception as e:
                report[sym] = f"error:{type(e).__name__}"
                continue
            self._depths[sym] = d
            chosen.append(sym)
            report[sym] = "ok"
        self._universe, self._universe_ts, self.universe_report = chosen, now, report


# ---------------------------------------------------------------------------- replay
class ReplayFeed(_FeedBase):
    """Pre-loaded 1m candles + a synthetic book around the last closed 1m close. `freeze()` stops
    the data (quotes age, no new bars) to exercise stale-feed handling."""

    def __init__(self, candles_1m: Mapping[str, Sequence[Candle]], rules: Mapping[str, SymbolRules],
                 config: Config, *, universe: Sequence[str] | None = None, spread_bps: Decimal = Decimal("4"),
                 levels: int = 5, level_notional: Decimal = Decimal("1000000"),
                 on_book: BookCallback | None = None) -> None:
        super().__init__(config)
        self.rules = dict(rules)
        self._m1 = {s: list(v) for s, v in candles_1m.items()}
        self._m1_ts = {s: [c.ts for c in v] for s, v in self._m1.items()}
        self._bars = {s: aggregate(v, BAR_MINUTES) for s, v in self._m1.items()}
        self._bar_ts = {s: [b.ts for b in v] for s, v in self._bars.items()}
        self._universe = sorted(universe if universe is not None else self._m1)
        self.spread_bps, self.levels, self.level_notional = spread_bps, levels, level_notional
        self.on_book = on_book
        self.frozen = False
        self._now: float | None = None
        self._quotes: dict[str, Quote] = {}
        self._depths: dict[str, Depth] = {}

    @property
    def span(self) -> tuple[int, int] | None:
        """(first 1m open, last 1m open) over all symbols."""
        ts = [t for v in self._m1_ts.values() for t in (v[:1] + v[-1:])]
        return (min(ts), max(ts)) if ts else None

    def freeze(self) -> None:
        self.frozen = True

    def unfreeze(self) -> None:
        self.frozen = False

    def symbols(self) -> list[str]:
        return list(self._universe)

    def track(self, symbols: Iterable[str]) -> None:
        pass  # every loaded symbol is always refreshed

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def depth(self, symbol: str) -> Depth | None:
        return self._depths.get(symbol)

    def closed_bars(self, symbol: str, n: int) -> list[Candle]:
        if self._now is None or symbol not in self._bars or n <= 0:
            return []
        j = bisect.bisect_right(self._bar_ts[symbol], self._now - BAR_S)
        return self._bars[symbol][max(0, j - n):j]

    def refresh(self, now: float) -> None:
        if self.frozen:
            return
        self._now = float(now)
        for sym, ts in self._m1_ts.items():
            i = bisect.bisect_right(ts, now - MIN_S)   # candles whose minute has closed
            if i == 0 or sym not in self.rules:
                continue
            bids, asks = self._book(self._m1[sym][i - 1].close, self.rules[sym].price_precision)
            self._quotes[sym] = Quote(sym, bids[0][0], asks[0][0], self._now)
            self._depths[sym] = Depth(sym, tuple(asks), tuple(bids), self._now)
            if self.on_book is not None:
                self.on_book(sym, bids, asks)

    def _book(self, px: Decimal, precision: int) -> tuple[list, list]:
        tick = Decimal(1).scaleb(-precision)
        half = px * self.spread_bps / 2 / _BPS
        bid = max(round_down(px - half, precision), tick)
        ask = (px + half).quantize(tick, rounding=ROUND_CEILING)
        if ask <= bid:
            ask = bid + tick
        qty = (self.level_notional / px).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        bids = [(bid - k * tick, qty) for k in range(self.levels) if bid - k * tick > 0]
        asks = [(ask + k * tick, qty) for k in range(self.levels)]
        return bids, asks
