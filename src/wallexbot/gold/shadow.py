"""Gold shadow: G1 through the repo's REAL bot path, never sending anything.

Both modes build the same stack the `bot` command builds (bot/main.py helpers): `bot.loop.Bot`
(entries, exits, persisted state) -> `pipeline.route_to_oms` -> `OMS` (risk engine, kill switch,
append-only SQLite audit store) -> `ShadowBroker` (simulated fills on an order book, local JSONL
journal), plus the in-process guardian. Only the market-data feed and the strategy objects differ:
the G1 engine / trail policy from gold/strategy.py and a 1h-bar feed.

- replay (default, offline): `GoldReplayFeed` replays the committed Binance PAXGUSDT data under a
  simulated clock: 1h bars for decisions (a bar is visible only once closed), 5m closes for quotes,
  and a synthetic book of +/- half_spread_bps around each quote (deep levels, so no impact). The bot
  is ticked on every 5m boundary and every 2 s while an order or exit is in flight.
- live (`--live`): `GoldLiveFeed` polls the Wallex public order book of the chosen market through
  the repo's host-pinned public client (GET only) and the decision bars from Binance (host-pinned
  GET, the series the backtest used) or Wallex; ShadowBroker simulates fills on the REAL Wallex book
  and logs the would-be orders. Nothing is sent: ShadowBroker has no network code at all.
"""
from __future__ import annotations

import bisect
import json
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from wallexbot.bot.feed import FeedError, _FeedBase
from wallexbot.bot.loop import Bot, BotParams
from wallexbot.bot.main import (GUARDIAN_INTERVAL_S, SimClock, build_stack, fresh_depth_source, make_broker,
                                make_guardian, pre_tick)
from wallexbot.config import DEFAULT_PATH, Config, load_config
from wallexbot.data.candles import Candle
from wallexbot.exchange.wallex_public import Depth
from wallexbot.gold import data as gdata
from wallexbot.gold.strategy import G1Engine, G1Params, G1Trail
from wallexbot.store import Store
from wallexbot.strategy.sizing import size_position
from wallexbot.types import Quote, SymbolRules, round_down

H1 = 3600
M5 = 300
SHADOW_DAYS = 30
FAST_STEP_S = 2.0
DEFAULT_RUN_DIR = Path("runs")


def synthetic_book(px: Decimal, precision: int, half_spread_bps: Decimal, levels: int = 5,
                   level_notional: Decimal = Decimal(10) ** 6) -> tuple[list, list]:
    """(bids, asks) best first: bid/ask at px -/+ half_spread_bps, rounded INWARD to the tick so the
    quoted spread never exceeds 2 x half_spread_bps (the risk engine's max_spread_bps is 15)."""
    tick = Decimal(1).scaleb(-precision)
    half = px * half_spread_bps / Decimal(10000)
    bid = (px - half).quantize(tick, rounding=ROUND_CEILING)
    ask = round_down(px + half, precision)
    if ask <= bid:
        bid, ask = round_down(px, precision), round_down(px, precision) + tick
    qty = round_down(level_notional / px, 8)
    return ([(bid - k * tick, qty) for k in range(levels) if bid - k * tick > 0],
            [(ask + k * tick, qty) for k in range(levels)])


class GoldReplayFeed(_FeedBase):
    """MarketFeed over committed 1h bars (decisions) and finer candles (quotes), offline."""

    def __init__(self, bars_1h: Sequence[Candle], quotes: Sequence[Candle], rules: Mapping[str, SymbolRules],
                 config: Config, *, symbol: str, quote_s: int = M5, half_spread_bps: Decimal = Decimal("7.5")):
        super().__init__(config)
        self.symbol, self.rules, self.quote_s, self.half_spread_bps = symbol, dict(rules), quote_s, half_spread_bps
        self._bars = tuple(bars_1h)
        self._bar_close = [b.ts + H1 for b in self._bars]
        self._quotes = tuple(quotes)
        self._quote_close = [c.ts + quote_s for c in self._quotes]
        self._now: float | None = None
        self._quote: Quote | None = None
        self._depth: Depth | None = None

    def symbols(self) -> list[str]:
        return [self.symbol]

    def track(self, symbols) -> None:
        pass

    def refresh(self, now: float) -> None:
        self._now = float(now)
        i = bisect.bisect_right(self._quote_close, self._now)   # quote candles CLOSED by now
        if i == 0:
            return
        bids, asks = synthetic_book(self._quotes[i - 1].close, self.rules[self.symbol].price_precision,
                                    self.half_spread_bps)
        self._quote = Quote(self.symbol, bids[0][0], asks[0][0], self._now)
        self._depth = Depth(self.symbol, tuple(asks), tuple(bids), self._now)

    def closed_bars(self, symbol: str, n: int) -> list[Candle]:
        if self._now is None or symbol != self.symbol or n <= 0:
            return []
        j = bisect.bisect_right(self._bar_close, self._now)
        return list(self._bars[max(0, j - n):j])

    def quote(self, symbol: str) -> Quote | None:
        return self._quote if symbol == self.symbol else None

    def depth(self, symbol: str) -> Depth | None:
        return self._depth if symbol == self.symbol else None


class GoldLiveFeed(_FeedBase):
    """MarketFeed over the live Wallex public book (repo client, GET only, host-pinned) and 1h
    decision bars from Binance (gold/binance.py, host-pinned GET) or from Wallex candles."""

    def __init__(self, client: Any, rules: Mapping[str, SymbolRules], config: Config, *, symbol: str,
                 bars_source: str = "binance", binance_transport=None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 12.0, candle_poll_s: float = 120.0,
                 history_days: int = 130):
        super().__init__(config)
        if bars_source not in ("binance", "wallex"):
            raise ValueError("bars_source must be 'binance' or 'wallex'")
        self.client, self.rules, self.symbol, self.bars_source = client, dict(rules), symbol, bars_source
        self.binance_transport, self.clock, self.sleep = binance_transport, clock, sleep
        self.poll_s, self.candle_poll_s, self.history_days = poll_s, candle_poll_s, history_days
        self._bars: dict[int, Candle] = {}
        self._depth: Depth | None = None
        self._last_depth: float | None = None
        self._last_bars: float | None = None
        self._now: float | None = None
        self.last_bars_error: str | None = None

    def symbols(self) -> list[str]:
        return [self.symbol]

    def track(self, symbols) -> None:
        pass

    def _fetch_bars(self, now: float) -> None:
        end = int(now) // H1 * H1 - H1
        start = (max(self._bars) - 2 * H1) if self._bars else end - self.history_days * 86400
        if self.bars_source == "binance":
            from wallexbot.gold.binance import fetch_klines
            new = fetch_klines(gdata.BINANCE_SYMBOL, "1h", start, end, transport=self.binance_transport,
                               clock=self.clock, sleep=lambda s: None)
        else:
            new = self.client.candles(self.symbol, "60", start, end)
        for c in new:
            self._bars[c.ts] = c

    def refresh(self, now: float) -> None:
        self._now = float(now)
        if self._last_bars is None or now - self._last_bars >= self.candle_poll_s:
            try:
                self._fetch_bars(now)
                self._last_bars, self.last_bars_error = now, None
            except Exception as e:   # keep the old bars; they age into "stale" in the bot
                self.last_bars_error = f"{type(e).__name__}: {e}"[:300]
        if self._last_depth is None or now - self._last_depth >= self.poll_s:
            try:
                d = self.client.depth(self.symbol)
            except Exception as e:
                raise FeedError(f"wallex depth {self.symbol}: {type(e).__name__}: {e}"[:300]) from e
            self._depth = Depth(self.symbol, d.asks, d.bids, float(now))
            self._last_depth = now

    def closed_bars(self, symbol: str, n: int) -> list[Candle]:
        if self._now is None or symbol != self.symbol or n <= 0:
            return []
        bars = [self._bars[t] for t in sorted(self._bars) if t + H1 <= self._now]
        return bars[-n:]

    def quote(self, symbol: str) -> Quote | None:
        return self._depth.quote if self._depth is not None and symbol == self.symbol else None

    def depth(self, symbol: str) -> Depth | None:
        return self._depth if symbol == self.symbol else None


def bot_params(p: G1Params) -> BotParams:
    """The repo's BotParams on a 1h bar, with G1's re-entry cooldown (24 bars after any exit)."""
    return BotParams(bar_s=H1, history_bars=p.lookback_bars, cooldown_bars=p.cooldown_bars,
                     cooldown_bars_profitable=p.cooldown_bars)


@dataclass
class ShadowStack:
    bot: Bot
    broker: Any
    store: Store
    guardian: Any
    feed: Any


def build_shadow_stack(feed, rules: Mapping[str, SymbolRules], config: Config, clock, sleep, db: Path,
                       journal: Path, *, params: G1Params, balance: Decimal, guardian: bool, monotonic,
                       mode_label: str) -> ShadowStack:
    broker = make_broker("shadow", config=config, clock=clock, rules=rules,
                         book_source=fresh_depth_source(feed, clock), balances={config.quote_currency: balance},
                         journal_path=journal)
    store = Store(db, clock=clock)
    ks, risk, oms = build_stack(store, broker, rules, config, clock)
    info = {"mode": mode_label, "strategy": "gold G1 (gold/README.md)", "broker": "ShadowBroker (no orders sent)",
            "guardian": {"enabled": guardian, "in_process": True}}
    bot = Bot(feed, G1Engine(params), G1Trail(params), size_position, oms, risk, store, ks, config, clock, None,
              params=bot_params(params), profile="strict", run_info=info)
    gd = make_guardian(guardian, db, broker, rules, config, clock, sleep, GUARDIAN_INTERVAL_S,
                       lambda: set(feed.symbols()) | set(bot.positions), monotonic=monotonic)
    return ShadowStack(bot, broker, store, gd, feed)


def _fresh_paths(db: Path, journal: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm"), journal):
        if p.exists():
            p.unlink()


def shadow_window(data: gdata.GoldData, days: int = SHADOW_DAYS) -> tuple[int, int]:
    """(start, end): the last `days` complete UTC days that the 5m quote data covers."""
    last_close = min(data.h1[-1].ts + H1, data.m5[-1].ts + M5)
    end = last_close // 86400 * 86400
    return end - days * 86400, end


def run_replay(data: gdata.GoldData, db: Path, journal: Path, *, start_ts: int, end_ts: int,
               params: G1Params | None = None, balance: Decimal = Decimal(1000), guardian: bool = True,
               symbol: str = "PAXGUSDT", half_spread_bps: Decimal = Decimal("7.5"),
               quotes: Sequence[Candle] | None = None, quote_s: int = M5) -> dict:
    """Offline replay of [start_ts, end_ts] through the bot path (quotes default: the 5m candles).
    Returns a summary dict."""
    params = params or G1Params()
    cfg = load_config(DEFAULT_PATH, env={})
    rules = {symbol: gdata.symbol_rules(data, symbol)}
    _fresh_paths(db, journal)
    clock = SimClock(start_ts)
    feed = GoldReplayFeed(data.h1, data.m5 if quotes is None else quotes, rules, cfg, symbol=symbol,
                          quote_s=quote_s, half_spread_bps=half_spread_bps)
    st = build_shadow_stack(feed, rules, cfg, clock, clock.advance, db, journal, params=params, balance=balance,
                            guardian=guardian, monotonic=clock, mode_label="shadow-replay (offline)")
    ticks = errors = 0
    try:
        while clock() <= end_ts:
            pre_tick(feed, st.broker, clock)
            rep = st.bot.tick()
            if st.guardian is not None:
                st.guardian.tick()
            ticks += 1
            errors += bool(rep.errors)
            busy = (st.broker.open_orders() or st.bot.plans
                    or any(tp.exit_request is not None for tp in st.bot.positions.values()))
            now = clock()
            nxt = (int(now) // quote_s + 1) * quote_s
            clock.advance(FAST_STEP_S if busy and now + FAST_STEP_S < nxt else nxt - now)
        return final_state(st, symbol, ticks, errors, rules[symbol])
    finally:
        if st.guardian is not None:
            st.guardian.close()
        st.store.close()


def final_state(st: ShadowStack, symbol: str, ticks: int, errors: int, rules: SymbolRules) -> dict:
    bal = st.broker.balances()
    q = st.feed.quote(symbol)
    usdt = bal["USDT"].total if "USDT" in bal else Decimal(0)
    base = bal[rules.base].total if rules.base in bal else Decimal(0)
    mark = q.bid if q is not None else Decimal(0)
    intents: dict[str, int] = {}
    for r in st.store.rows("order_intents"):
        intents[r["state"]] = intents.get(r["state"], 0) + 1
    return {"ticks": ticks, "ticks_with_errors": errors, "usdt": str(usdt), "base_qty": str(base),
            "mark_bid": str(mark), "equity_at_bid": float(usdt + base * mark),
            "kill_switch_engaged": st.bot.ks.is_engaged(), "open_positions": sorted(st.bot.positions),
            "intents_by_state": dict(sorted(intents.items())), "errors_rows": len(st.store.rows("errors")),
            "decisions": decision_counts(st.store),
            "guardian": None if st.guardian is None else {"ticks": st.guardian.ticks, "failed": st.guardian.failed_ticks}}


def decision_counts(store: Store) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in store.rows("audit_events"):
        out[r["kind"]] = out.get(r["kind"], 0) + 1
    return dict(sorted(out.items()))


def journal_fills(journal: Path) -> list[dict]:
    if not journal.is_file():
        return []
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r["event"] == "fill"]


def run_live(db: Path, journal: Path, *, symbol: str = "PAXGUSDT", bars_source: str = "binance",
             max_ticks: int | None = None, interval_s: float = 12.0, client: Any = None, binance_transport=None,
             clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
             balance: Decimal = Decimal(1000), guardian: bool = True, params: G1Params | None = None,
             rules: SymbolRules | None = None, log: Callable[[str], None] = print) -> dict:
    """Live shadow: polls the Wallex public book, logs would-be orders (ShadowBroker journal + audit
    store). Sends nothing. `client` / `binance_transport` are injectable for offline smoke tests."""
    from wallexbot.exchange.wallex_public import WallexPublicClient
    params = params or G1Params()
    cfg = load_config(DEFAULT_PATH, env={})
    client = client or WallexPublicClient(clock=clock, sleep=sleep)
    if rules is None:
        found = {r.symbol: r for r in client.markets()}
        if symbol not in found:
            raise ValueError(f"{symbol} is not a Wallex spot market")
        rules = found[symbol]
    rule_map = {symbol: rules}
    db.parent.mkdir(parents=True, exist_ok=True)
    feed = GoldLiveFeed(client, rule_map, cfg, symbol=symbol, bars_source=bars_source,
                        binance_transport=binance_transport, clock=clock, sleep=sleep)
    st = build_shadow_stack(feed, rule_map, cfg, clock, sleep, db, journal, params=params, balance=balance,
                            guardian=guardian, monotonic=None, mode_label="shadow-live (Wallex public book)")
    ticks = errors = 0
    try:
        while max_ticks is None or ticks < max_ticks:
            pre_tick(feed, st.broker, clock)
            rep = st.bot.tick()
            if st.guardian is not None:
                st.guardian.tick()
            ticks += 1
            errors += bool(rep.errors)
            q = feed.quote(symbol)
            log(f"tick {ticks}: bid={q.bid if q else None} ask={q.ask if q else None} "
                f"bars={len(feed.closed_bars(symbol, 10 ** 6))} actions={rep.actions} errors={rep.errors} "
                f"degraded={rep.degraded}" + (f" bars_error={feed.last_bars_error}" if feed.last_bars_error else ""))
            if max_ticks is not None and ticks >= max_ticks:
                break
            sleep(interval_s)
        return final_state(st, symbol, ticks, errors, rules)
    finally:
        if st.guardian is not None:
            st.guardian.close()
        st.store.close()
