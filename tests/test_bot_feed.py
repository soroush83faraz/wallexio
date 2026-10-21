"""MarketFeed implementations: PublicFeed (fake public client, no network) and ReplayFeed."""
import dataclasses
from decimal import Decimal

import pytest
from test_bot_helpers import RULES, SYM, T0, WINNER, cheap_config, series

from wallexbot.bot.feed import (FeedError, FeedParams, PublicFeed, ReplayFeed, explode_bars, longest_zero_volume_run,
                                notional_within, slippage_bps)
from wallexbot.data.aggregate import aggregate
from wallexbot.data.candles import Candle
from wallexbot.exchange.mock_wallex import MockClock
from wallexbot.exchange.wallex_public import Depth
from wallexbot.types import SymbolRules

D = Decimal


class FakeClient:
    """Duck-typed WallexPublicClient: rank_universe / depth / candles / markets, all in memory."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.books = {}            # symbol -> (bid, ask, qty)
        self.ranked = []
        self.fail = set()
        self.zero = set()          # symbols whose candles have zero volume

    def markets(self):
        return list(self.ranked)

    def rank_universe(self, quote, n):
        self.calls.append(("rank", n))
        return self.ranked[:n]

    def depth(self, sym):
        self.calls.append(("depth", sym))
        if sym in self.fail:
            raise OSError("timeout")
        bid, ask, qty = self.books[sym]
        return Depth(sym, ((D(ask), D(qty)), (D(ask) + 1, D(qty))), ((D(bid), D(qty)),), self.clock())

    def candles(self, sym, res, start, end):
        self.calls.append(("candles", sym, start, end))
        if sym in self.fail:
            raise OSError("timeout")
        vol = D(0) if sym in self.zero else D(1)
        return [Candle(t, D(100), D(101), D(99), D(100), vol) for t in range(start, end + 1, 60)]


def _rules(sym):
    return SymbolRules(sym, sym[:-4], "USDT", 4, 2)


@pytest.fixture
def cfg():
    return dataclasses.replace(cheap_config(), top_n=2)


def test_universe_filters_spread_depth_and_zero_volume(cfg):
    clock = MockClock(T0)
    c = FakeClient(clock)
    c.ranked = [_rules(s) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT")]
    c.books = {"AAAUSDT": ("100", "100.01", "1000"), "BBBUSDT": ("100", "101", "1000"),   # 99 bps spread
               "CCCUSDT": ("100", "100.01", "1"),                                          # too thin
               "DDDUSDT": ("100", "100.01", "1000"), "EEEUSDT": ("100", "100.01", "1000")}
    c.zero = {"DDDUSDT"}
    feed = PublicFeed(c, cfg)
    feed.refresh(clock())
    assert feed.symbols() == ["AAAUSDT"]
    assert ("rank", 4) in c.calls                                # top_n x oversample
    rep = feed.universe_report
    assert rep["AAAUSDT"] == "ok" and rep["BBBUSDT"].startswith("spread")
    assert rep["CCCUSDT"].startswith("ask depth") and rep["DDDUSDT"].startswith("zero-volume")
    assert "EEEUSDT" not in rep                                  # only 4 candidates were ranked
    # refreshed daily, not on every poll
    clock.advance(3600)
    feed.refresh(clock())
    assert sum(1 for x in c.calls if x[0] == "rank") == 1
    clock.advance(86400)
    feed.refresh(clock())
    assert sum(1 for x in c.calls if x[0] == "rank") == 2


def test_incremental_candles_closed_bars_and_quotes(cfg):
    clock = MockClock(T0 + 7 * 60 + 13)
    c = FakeClient(clock)
    c.ranked = [_rules(SYM)]
    c.books = {SYM: ("100", "100.01", "1000")}
    feed = PublicFeed(c, cfg, params=FeedParams(history_bars=8, max_requests_per_refresh=100))
    feed.refresh(clock())
    first = [x for x in c.calls if x[0] == "candles"]
    end = (int(clock()) // 60) * 60 - 60
    assert first[-1][3] == end                                   # newest CLOSED minute only
    bars = feed.closed_bars(SYM, 100)
    assert bars and all(b.ts % 900 == 0 and b.ts + 900 <= clock() for b in bars)
    assert bars[-1].missing == 0
    q = feed.quote(SYM)
    assert q.bid == D("100") and q.ask == D("100.01") and q.ts == clock()
    n_calls = len(c.calls)
    clock.advance(5)                                             # nothing due yet
    feed.refresh(clock())
    assert len(c.calls) == n_calls
    clock.advance(60)
    feed.refresh(clock())
    inc = [x for x in c.calls[n_calls:] if x[0] == "candles"]
    assert len(inc) == 1 and inc[0][2] == end + 60               # only the new minute(s)
    # a failing depth poll keeps the old quote, which ages into stale
    c.fail.add(SYM)
    old = feed.quote(SYM).ts
    clock.advance(30)
    with pytest.raises(FeedError):
        feed.refresh(clock())
    assert feed.quote(SYM).ts == old
    ctx = feed.market_context(SYM)
    assert ctx.taker_fee_rate == cfg.taker_rate and ctx.spread_bps == D("1.0000")


def test_request_budget_per_refresh(cfg):
    clock = MockClock(T0)
    c = FakeClient(clock)
    c.ranked = [_rules(s) for s in ("AAAUSDT", "EEEUSDT")]
    c.books = {s: ("100", "100.01", "1000") for s in ("AAAUSDT", "EEEUSDT", "XXXUSDT")}
    feed = PublicFeed(c, cfg, params=FeedParams(max_requests_per_refresh=3, history_bars=8))
    feed.track(["XXXUSDT"])                                      # an open position outside the universe
    feed.refresh(clock())
    polled = [x for x in c.calls if x[0] in ("depth", "candles")]
    universe_calls = 2 * 2                                       # depth + candles per universe candidate
    assert len(polled) - universe_calls <= 3
    clock.advance(60)
    feed.refresh(clock())
    assert feed.quote("XXXUSDT") is not None                     # tracked symbols are polled too


def test_replay_feed_closed_bars_only_and_synthetic_book():
    cfg = cheap_config()
    bars = series(WINNER)
    books = []
    feed = ReplayFeed({SYM: explode_bars(bars)}, RULES, cfg, on_book=lambda s, b, a: books.append((s, b, a)))
    b = bars[130]
    feed.refresh(b.ts + 900 - 1)
    assert feed.closed_bars(SYM, 1000)[-1].ts == bars[129].ts   # bar 130 not closed yet
    feed.refresh(b.ts + 900)
    got = feed.closed_bars(SYM, 1000)
    assert got[-1] == b and got == bars[:131]
    q = feed.quote(SYM)
    assert q.ts == b.ts + 900 and q.bid < b.close < q.ask and q.spread_bps <= D("6")
    assert books[-1][0] == SYM and books[-1][1][0][0] == q.bid and books[-1][2][0][0] == q.ask
    feed.freeze()
    feed.refresh(b.ts + 2000)
    assert feed.quote(SYM).ts == b.ts + 900 and feed.closed_bars(SYM, 1000)[-1] == b
    ctx = feed.market_context(SYM)
    assert ctx.est_slippage_bps == 0 and ctx.taker_fee_rate == cfg.taker_rate


def test_helpers():
    levels = [(D("100"), D("1")), (D("101"), D("1"))]
    assert slippage_bps(levels, D("100")) == 0
    assert D("49") < slippage_bps(levels, D("201")) < D("51")
    assert slippage_bps(levels, D("1000")) is None
    assert notional_within(levels, D("100.5"), asks=True) == D("100")
    bars = series(WINNER)[:20]
    assert aggregate(explode_bars(bars), 15) == bars[:-1] + [bars[-1]]
    m1 = explode_bars(bars[:1])
    assert longest_zero_volume_run(m1) == 14
