"""Shared rig for the bot tests (no tests of its own). Offline: ReplayFeed / stub feeds + MockWallex."""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path

from test_strategy_helpers import STEP, make_bars, wobble
from test_strategy_helpers import T0 as _T_HELPERS

from wallexbot.bot.feed import ReplayFeed, explode_bars
from wallexbot.bot.loop import BotParams
from wallexbot.bot.main import build_bot, build_stack
from wallexbot.config import load_config
from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.exchange.wallex_public import Depth
from wallexbot.store import Store
from wallexbot.strategy.contract import MarketContext
from wallexbot.types import IntentState, Quote, SymbolRules

D = Decimal
SYM = "BTCUSDT"
RULES = {SYM: SymbolRules(SYM, "BTC", "USDT", amount_precision=4, price_precision=2)}
T0 = 1_699_999_200                     # epoch-aligned to 15m (the strategy helpers' T0 is not)
SIGNAL_BAR = 124                       # breakout bar index of the series below
ENTRY_TS = T0 + (SIGNAL_BAR + 1) * STEP  # signal bar closes here


def cheap_config():
    """Default limits with a permissive 0.1 % fee so the S1 cost gate passes on the synthetic series."""
    return dataclasses.replace(load_config(env={}), taker_rate=D("0.001"), maker_rate=D("0.001"))


def closes_with(continuation):
    """breakout_series() closes (120 range bars + 5 accelerating bars) followed by `continuation`."""
    closes = [100.0 * (1 + wobble(i)) for i in range(120)]
    for r in (0.002, 0.003, 0.004, 0.006, 0.010):
        closes.append(closes[-1] * (1 + r))
    return closes + list(continuation)


def series(continuation=(), spike=3.0):
    closes = closes_with(continuation)
    vols = [1000.0 * (1 + 0.1 * ((i * 5) % 7 - 3) / 3) for i in range(len(closes))]
    vols[SIGNAL_BAR] = 1000.0 * spike
    return [dataclasses.replace(c, ts=c.ts - _T_HELPERS + T0) for c in make_bars(closes, vols)]


# after the breakout: a rise through +1R (partial take-profit), then a roll-over (momentum failure)
WINNER = (103.2, 103.9, 104.3, 104.6, 104.4, 103.6, 102.9, 102.5, 102.4, 102.3, 102.2, 102.1, 102.0, 101.9)
# after the breakout: straight down through the stop
LOSER = (102.3, 101.6, 100.8, 100.5, 100.4, 100.3, 100.2, 100.1, 100.0, 100.1, 100.2, 100.1, 100.0, 100.0)


class Rig:
    """One bot over a ReplayFeed + MockWallex sharing a MockClock. restart() simulates a process
    crash: a new Store connection, OMS, risk engine and Bot over the same DB and the same exchange."""

    def __init__(self, tmp_path: Path, bars=None, *, advisor=None, params=None, feed=None, config=None,
                 start: float | None = None, balance="10000"):
        self.tmp_path = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.cfg = config or cheap_config()
        self.clock = MockClock(start if start is not None else T0 + 121 * STEP)
        self.broker = MockWallex(RULES.values(), clock=self.clock, balances={"USDT": D(balance)},
                                 maker_rate=self.cfg.maker_rate, taker_rate=self.cfg.taker_rate)
        self.feed = feed if feed is not None else ReplayFeed(
            {SYM: explode_bars(bars if bars is not None else series(WINNER))}, RULES, self.cfg,
            on_book=self.broker.set_book)
        self.advisor, self.params = advisor, params or BotParams()
        self._open()

    def _open(self):
        self.store = Store(self.tmp_path / "bot.db", clock=self.clock)
        self.ks, self.risk, self.oms = build_stack(self.store, self.broker, RULES, self.cfg, self.clock)
        self.bot = build_bot(self.feed, self.oms, self.risk, self.store, self.ks, self.cfg, self.clock,
                             advisor=self.advisor, params=self.params)

    def restart(self, advance: float = 0.0):
        self.store.close()
        self.clock.advance(advance)
        self._open()

    def tick(self):
        return self.bot.tick()

    def run_until(self, t: float, step: float = 60.0):
        reps = []
        while self.clock() < t:
            reps.append(self.bot.tick())
            self.clock.advance(step)
        return reps

    def intents(self, side=None):
        out = self.store.intents_in_states(frozenset(IntentState))
        return [r for r in out if side is None or r.intent.side is side]

    def events(self, kind=None):
        return self.store.audit_events(actor="bot", kind=kind)

    def close(self):
        self.store.close()


class ManualFeed:
    """Hand-driven MarketFeed for unit-style tests: set bars, quotes and staleness directly."""

    def __init__(self, cfg, symbols=(SYM,)):
        self.cfg = cfg
        self.universe = list(symbols)
        self.bars: dict[str, list] = {s: [] for s in symbols}
        self.quotes: dict[str, Quote] = {}
        self.refresh_error: Exception | None = None
        self.refreshes = 0
        self.tracked: list[str] = []

    def symbols(self):
        return list(self.universe)

    def track(self, symbols):
        self.tracked = list(symbols)

    def refresh(self, now):
        self.refreshes += 1
        if self.refresh_error is not None:
            raise self.refresh_error

    def closed_bars(self, symbol, n):
        return self.bars.get(symbol, [])[-n:]

    def set_quote(self, symbol, bid, ask, ts):
        self.quotes[symbol] = Quote(symbol, D(bid), D(ask), ts)

    def quote(self, symbol):
        return self.quotes.get(symbol)

    def depth(self, symbol):
        q = self.quotes.get(symbol)
        return None if q is None else Depth(symbol, ((q.ask, D("100000")),), ((q.bid, D("100000")),), q.ts)

    def market_context(self, symbol, notional=None):
        q = self.quotes.get(symbol)
        return None if q is None else MarketContext(q.spread_bps, D("0"), self.cfg.taker_rate)
