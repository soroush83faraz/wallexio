"""Bot entry point. Mock and shadow modes only: live trading is NOT wired into the bot and is refused.

    python -m wallexbot.bot.main --db PATH --mode mock|shadow [--replay-from DIR] [--max-ticks N]
                                 [--interval-s S] [--config PATH] [--balance USDT]

- `--mode mock --replay-from DIR`: fully offline paper run. DIR uses the data.history layout
  (<DIR>/<SYMBOL>/<SYMBOL>_1m_<YYYY-MM>.csv + .sha256). ReplayFeed drives a paper MockWallex
  (book mirrored from the replay) under an accelerated simulated clock: each tick advances it by
  --interval-s simulated seconds (default 60). Symbol rules are inferred (amount precision 6,
  price precision from the data).
- `--mode mock` without replay: PublicFeed (public GETs to api.wallex.ir only) mirrored into a
  paper MockWallex; `--mode shadow`: PublicFeed + ShadowBroker (exchange/shadow.py, simulated
  fills against the live public book). --interval-s is wall-clock seconds between ticks (12).
- SIGINT / SIGTERM stop the loop after the current tick.

The guardian is a SEPARATE process on the same database (it watches this loop's "strategy"
heartbeat, loss limits and ledger-vs-exchange, and flattens on a hard breach):
    python -m wallexbot.guardian.main --db PATH --mode mock|shadow
(In mock/replay runs the paper broker lives inside this process, so a separately started guardian
sees its own demo broker, not this one; the ledger, heartbeat and kill switch are shared via the DB.)
"""
from __future__ import annotations

import argparse
import calendar
import logging
import re
import signal
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from wallexbot.bot.feed import BAR_S, MIN_S, PublicFeed, ReplayFeed
from wallexbot.bot.loop import Bot, BotParams
from wallexbot.config import DEFAULT_PATH, Config, ConfigError, load_config
from wallexbot.data.candles import Candle
from wallexbot.data.history import load_history
from wallexbot.oms import OMS
from wallexbot.pipeline import auto_approve, build_paper_broker
from wallexbot.risk.engine import RiskEngine
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import Store
from wallexbot.strategy.config import MomentumParams
from wallexbot.strategy.exits import ExitParams, make_policy
from wallexbot.strategy.momentum import MomentumSignalEngine
from wallexbot.strategy.sizing import size_position
from wallexbot.types import IntentState, SymbolRules

ALLOWED_MODES = ("mock", "shadow")
GUARDIAN_HINT = "run the guardian as a separate process: python -m wallexbot.guardian.main --db {db} --mode {mode}"
_MONTH_FILE = re.compile(r"^(?P<sym>[A-Z0-9]{3,20})_1m_(?P<y>\d{4})-(?P<m>\d{2})\.csv$")
log = logging.getLogger("wallexbot.bot")


class BotSetupError(RuntimeError):
    pass


class SimClock:
    """Accelerated simulated clock for replay runs."""

    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class StopFlag:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def set(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def wait(self, seconds: float) -> None:
        self._ev.wait(max(seconds, 0.0))


# ---------------------------------------------------------------------------- wiring
def make_broker(mode: str, *, config: Config, clock: Callable[[], float], rules: Mapping[str, SymbolRules],
                book_source: Callable[[str], Any] | None = None, balances: Mapping[str, Decimal] | None = None):
    """The broker object for a mode. mock: paper MockWallex (built on the order-path side, in
    pipeline.py). shadow: ShadowBroker over the live public book, imported lazily. Anything else
    (live in particular) is refused."""
    bal = dict(balances) if balances is not None else {config.quote_currency: Decimal("10000")}
    if mode == "mock":
        return build_paper_broker(config, clock, rules, bal)
    if mode == "shadow":
        try:
            from wallexbot.exchange.shadow import ShadowBroker
        except ImportError as e:
            raise BotSetupError("shadow mode needs wallexbot.exchange.shadow.ShadowBroker, which is not "
                                "available in this build") from e
        if book_source is None:
            raise BotSetupError("shadow mode needs a book source (public depth snapshots)")
        return ShadowBroker(book_source, rules.values(), bal, config.maker_rate, config.taker_rate, clock)
    raise BotSetupError(f"mode {mode!r} is not wired into the bot (allowed: {', '.join(ALLOWED_MODES)})")


def build_stack(store: Store, broker, rules: Mapping[str, SymbolRules], config: Config, clock):
    """(KillSwitch, RiskEngine, OMS) with the autonomous risk-gated approval policy."""
    ks = KillSwitch(store)
    risk = RiskEngine(config, rules, ks)
    oms = OMS(store, broker, risk, ks, approve=auto_approve, clock=clock)
    return ks, risk, oms


def build_bot(feed, oms, risk, store, ks, config: Config, clock, *, advisor: Any = None,
              params: BotParams | None = None, exit_policy: str = "E4") -> Bot:
    """S1 momentum entries, E4 hybrid exits (fee rate from config), risk-based sizing."""
    policy = make_policy(exit_policy, ExitParams(fee_rate=config.taker_rate))
    return Bot(feed, MomentumSignalEngine(), policy, size_position, oms, risk, store, ks, config, clock, advisor,
               params=params)


def load_replay_dir(path: str | Path, symbols: Sequence[str] | None = None) -> dict[str, list[Candle]]:
    """1m candles per symbol from a data.history directory (checksum-verified by load_history)."""
    root = Path(path)
    if not root.is_dir():
        raise BotSetupError(f"replay directory not found: {root}")
    out: dict[str, list[Candle]] = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        months = sorted((int(m["y"]), int(m["m"])) for f in d.iterdir()
                        if (m := _MONTH_FILE.match(f.name)) and m["sym"] == d.name)
        if not months or (symbols and d.name not in symbols):
            continue
        (y0, m0), (y1, m1) = months[0], months[-1]
        start = calendar.timegm((y0, m0, 1, 0, 0, 0)) - 86400 * 2
        end = calendar.timegm((y1 + (m1 == 12), m1 % 12 + 1, 1, 0, 0, 0)) + 86400 * 2
        candles = load_history(root, d.name, start, end)
        if candles:
            out[d.name] = candles
    return out


def infer_rules(candles: Mapping[str, Sequence[Candle]], quote_ccy: str, amount_precision: int = 6) -> dict[str, SymbolRules]:
    """Offline replay only: price precision = finest decimal seen in the closes (<= 8)."""
    rules = {}
    for sym, cs in candles.items():
        exps = [c.close.as_tuple().exponent for c in cs[:2000]]
        pp = min(8, max(0, -min(e for e in exps if isinstance(e, int)))) if exps else 2
        base = sym[: -len(quote_ccy)] if sym.endswith(quote_ccy) and len(sym) > len(quote_ccy) else sym
        rules[sym] = SymbolRules(sym, base, quote_ccy, amount_precision=amount_precision, price_precision=pp)
    return rules


def summarize(store: Store, bot: Bot, ticks: int) -> dict[str, Any]:
    intents = store.intents_in_states(frozenset(IntentState))
    by_state: dict[str, int] = {}
    for r in intents:
        by_state[r.state.value] = by_state.get(r.state.value, 0) + 1
    positions = store.latest_positions()
    return {"ticks": ticks, "intents": len(intents), "intents_by_state": by_state,
            "fills": len(store.rows("fills")), "signals": len(store.rows("signals")),
            "realized_pnl": {s: str(p.realized_pnl) for s, p in sorted(positions.items())},
            "open_positions": {s: str(p.quantity) for s, p in sorted(positions.items()) if p.quantity > 0},
            "tracked": sorted(bot.positions), "kill_switch_engaged": bot.ks.is_engaged(),
            "consecutive_failures": bot.consecutive_failures}


# ---------------------------------------------------------------------------- runners
def run_replay(db: str | Path, replay_dir: str | Path, config: Config, *, step_s: float = 60.0,
               max_ticks: int | None = None, balances: Mapping[str, Decimal] | None = None,
               advisor: Any = None, params: BotParams | None = None, stop: StopFlag | None = None,
               symbols: Sequence[str] | None = None, on_tick: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """Offline end-to-end paper run over recorded 1m candles under an accelerated clock."""
    candles = load_replay_dir(replay_dir, symbols)
    if not candles:
        raise BotSetupError(f"no 1m candle files found under {replay_dir}")
    stop = stop or StopFlag()
    rules = infer_rules(candles, config.quote_currency)
    first = min(cs[0].ts for cs in candles.values())
    last = max(cs[-1].ts for cs in candles.values())
    warmup = (MomentumParams.min_bars + 1) * BAR_S
    clock = SimClock((first + warmup) // MIN_S * MIN_S)
    broker = make_broker("mock", config=config, clock=clock, rules=rules, balances=balances)
    feed = ReplayFeed(candles, rules, config, on_book=broker.set_book)
    store = Store(db, clock=clock)
    try:
        ks, risk, oms = build_stack(store, broker, rules, config, clock)
        bot = build_bot(feed, oms, risk, store, ks, config, clock, advisor=advisor, params=params)
        ticks = 0
        while clock() <= last + MIN_S and not stop.is_set() and (max_ticks is None or ticks < max_ticks):
            rep = bot.tick()
            ticks += 1
            if on_tick is not None:
                on_tick(rep)
            clock.advance(step_s)
        return summarize(store, bot, ticks)
    finally:
        store.close()


def run_feed_loop(mode: str, db: str | Path, config: Config, *, interval_s: float = 12.0,
                  max_ticks: int | None = None, client: Any = None, balances: Mapping[str, Decimal] | None = None,
                  advisor: Any = None, stop: StopFlag | None = None, clock: Callable[[], float] = time.time,
                  sleep: Callable[[float], Any] | None = None,
                  on_tick: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """mock (paper broker mirrored from the public book) or shadow (ShadowBroker) on PublicFeed."""
    stop = stop or StopFlag()
    sleep = sleep or stop.wait
    if client is None:
        from wallexbot.exchange.wallex_public import WallexPublicClient
        client = WallexPublicClient()
    rules = {r.symbol: r for r in client.markets() if r.quote == config.quote_currency}
    feed = PublicFeed(client, config)
    broker = make_broker(mode, config=config, clock=clock, rules=rules, book_source=feed.depth, balances=balances)
    if mode == "mock":
        feed.on_book = broker.set_book   # paper fills at the live public prices
    store = Store(db, clock=clock)
    try:
        ks, risk, oms = build_stack(store, broker, rules, config, clock)
        bot = build_bot(feed, oms, risk, store, ks, config, clock, advisor=advisor)
        ticks = 0
        while not stop.is_set() and (max_ticks is None or ticks < max_ticks):
            if hasattr(broker, "step"):        # ShadowBroker matches resting orders on step()
                try:
                    broker.step(clock())
                except Exception:
                    log.exception("bot: broker step failed")
            rep = bot.tick()
            ticks += 1
            if on_tick is not None:
                on_tick(rep)
            if stop.is_set() or (max_ticks is not None and ticks >= max_ticks):
                break
            sleep(interval_s)
        return summarize(store, bot, ticks)
    finally:
        store.close()


# ---------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wallexbot.bot.main",
                                description="Autonomous trading loop (mock / shadow only; live is refused).")
    p.add_argument("--db", required=True, help="path of the audit SQLite database")
    p.add_argument("--mode", required=True, help="mock | shadow (live is refused)")
    p.add_argument("--replay-from", default=None, help="data.history directory for an offline replay (mock)")
    p.add_argument("--max-ticks", type=int, default=None, help="stop after N ticks")
    p.add_argument("--interval-s", type=float, default=None,
                   help="replay: simulated seconds per tick (60); otherwise wall seconds between ticks (12)")
    p.add_argument("--config", default=None, help="config YAML (default: config/default.yaml)")
    p.add_argument("--balance", default="10000", help="starting paper balance in the quote currency")
    args = p.parse_args(argv)
    if args.mode == "live":
        print("refused: live trading is not wired into the bot; use --mode mock or --mode shadow",
              file=sys.stderr)
        return 2
    if args.mode not in ALLOWED_MODES:
        print(f"refused: mode {args.mode!r} is not allowed (only {', '.join(ALLOWED_MODES)})", file=sys.stderr)
        return 2
    if args.replay_from and args.mode != "mock":
        print("refused: --replay-from runs against the paper broker; use --mode mock", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config or DEFAULT_PATH, env={})   # never armed: no live path from here
        balance = Decimal(args.balance)
    except (ConfigError, OSError, ArithmeticError, ValueError, KeyError) as e:
        print(f"refused: bad configuration: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print(GUARDIAN_HINT.format(db=args.db, mode=args.mode), flush=True)
    stop = StopFlag()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())

    def show(rep) -> None:
        if rep.actions or rep.errors:
            print(f"tick ts={rep.ts:.0f} actions={rep.actions} errors={rep.errors}", flush=True)

    try:
        balances = {cfg.quote_currency: balance}
        if args.replay_from:
            summary = run_replay(args.db, args.replay_from, cfg, step_s=args.interval_s or 60.0,
                                 max_ticks=args.max_ticks, balances=balances, stop=stop, on_tick=show)
        else:
            summary = run_feed_loop(args.mode, args.db, cfg, interval_s=args.interval_s or 12.0,
                                    max_ticks=args.max_ticks, balances=balances, stop=stop, on_tick=show)
    except BotSetupError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"bot stopped: {summary}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
