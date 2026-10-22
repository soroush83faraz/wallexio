"""Bot entry point. Mock and shadow modes only: live trading is NOT wired into the bot and is refused.

    python -m wallexbot.bot.main --db PATH --mode mock|shadow [--replay-from DIR] [--max-ticks N]
                                 [--interval-s S] [--config PATH] [--balance USDT]
                                 [--profile strict|demo] [--no-guardian] [--guardian-interval-s S]

- `--mode mock --replay-from DIR`: fully offline paper run. DIR uses the data.history layout
  (<DIR>/<SYMBOL>/<SYMBOL>_1m_<YYYY-MM>.csv + .sha256). ReplayFeed drives a paper MockWallex
  (book mirrored from the replay) under an accelerated simulated clock: each tick advances it by
  --interval-s simulated seconds (default 60). Symbol rules are inferred (amount precision 6,
  price precision from the data).
- `--mode mock` without replay: PublicFeed (public GETs to api.wallex.ir only) mirrored into a
  paper MockWallex; `--mode shadow`: PublicFeed + ShadowBroker (exchange/shadow.py, simulated
  fills against the live public book). --interval-s is wall-clock seconds between ticks (12); it
  must be <= max_data_age_s / 2 (15 s by default), else exit 2 (interval_problem()): the feed
  refreshes only on bot ticks, and this bound keeps every quote the guardian sees, on its ticks
  in between included, younger than max_data_age_s.
- Replay: --interval-s is SIMULATED seconds per tick, in (0, 900]; the replay feed is re-stamped
  every tick and the guardian ticks at the same simulated instant, so it cannot age.
- SIGINT / SIGTERM stop the loop after the current tick.

Guardian (default on; --no-guardian disables it): an InProcessGuardian (guardian/main.py) with its
own Store connection on the same database and the SAME broker object as the bot, ticked after every
bot tick and, while the bot sleeps longer than --guardian-interval-s (15), in between. It watches
the "strategy" heartbeat, loss limits and ledger-vs-broker balances, engages the kill switch and
flattens on a hard breach, through the OMS, against the paper/shadow broker. --stale-action
hold|flatten (default hold) picks what it does with a held position whose quote has been stale or
missing for > 120 s; either way it re-alerts every 5 min while that lasts. In shadow mode this
EXERCISES the guardian logic on simulated fills. A separate guardian process would only make sense
against a live exchange account, which this build does not have.

Profiles (--profile): `strict` (default) is S1 as specified; `demo` is an EXERCISE profile with
relaxed thresholds (strategy/profiles.py) so that mock/shadow runs show a full lifecycle. It prints
EXERCISE_BANNER, is written into every bot audit event and the healthcheck output, and is refused
for anything but mock/shadow.
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
from wallexbot.config import Config, ConfigError, load_config
from wallexbot.data.candles import Candle
from wallexbot.data.history import load_history
from wallexbot.guardian.main import DEFAULT_INTERVAL_S as GUARDIAN_INTERVAL_S
from wallexbot.guardian.main import STALE_ACTIONS, GuardianConfig, InProcessGuardian
from wallexbot.oms import OMS
from wallexbot.pipeline import auto_approve, build_paper_broker
from wallexbot.risk.engine import RiskEngine
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import Store
from wallexbot.strategy.config import MomentumParams
from wallexbot.strategy.exits import ExitParams, make_policy
from wallexbot.strategy.momentum import MomentumSignalEngine
from wallexbot.strategy.profiles import EXERCISE_BANNER, PROFILES, ProfileError, momentum_params
from wallexbot.strategy.sizing import size_position
from wallexbot.types import IntentState, SymbolRules

ALLOWED_MODES = ("mock", "shadow")
DEFAULT_BALANCE = Decimal("1000")        # USDT; override with --balance
BOOK_MAX_AGE_S = 60.0                    # a cached public depth older than this is not a book to simulate fills on
JOURNAL_NAME = "shadow_journal.jsonl"    # next to the database
GUARDIAN_ON = ("guardian: in-process (same broker object, own DB connection), tick after every bot tick, "
               "at least every {interval:g}s while the bot sleeps")
GUARDIAN_OFF = "guardian: DISABLED (--no-guardian): no loss-limit, heartbeat or balance watchdog in this run"
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
                book_source: Callable[[str], Any] | None = None, balances: Mapping[str, Decimal] | None = None,
                journal_path: str | Path | None = None):
    """The broker object for a mode. mock: paper MockWallex (built on the order-path side, in
    pipeline.py). shadow: ShadowBroker over the live public book, imported lazily. Anything else
    (live in particular) is refused."""
    bal = dict(balances) if balances is not None else {config.quote_currency: DEFAULT_BALANCE}
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
        return ShadowBroker(book_source, rules.values(), bal, config.maker_rate, config.taker_rate, clock,
                            journal_path=journal_path)
    raise BotSetupError(f"mode {mode!r} is not wired into the bot (allowed: {', '.join(ALLOWED_MODES)})")


def fresh_depth_source(feed, clock: Callable[[], float], max_age_s: float = BOOK_MAX_AGE_S) -> Callable[[str], Any]:
    """Book source for ShadowBroker that REUSES the feed's cached public depth (no extra requests).
    No book, one older than `max_age_s`, or one dated after the clock (its ts is the local receive
    time, so a future ts means the clock stepped back or the data is bad) raises: the broker then
    books a book_error and moves nothing."""
    def source(symbol: str):
        d = feed.depth(symbol)
        if d is None:
            raise LookupError(f"no depth cached for {symbol}")
        age = float(clock()) - float(d.ts)
        if not age >= 0:       # also NaN
            raise LookupError(f"cached depth for {symbol} is dated after the clock; not a valid book")
        if age > max_age_s:
            raise LookupError(f"cached depth for {symbol} is stale ({age:.0f}s old)")
        return d
    return source


def pre_tick(feed, broker, clock: Callable[[], float]) -> None:
    """Before each tick of a broker that simulates over time (ShadowBroker): refresh the feed (depth
    requests are rate-limited per symbol, so the tick's own refresh then costs nothing) and let the
    broker match its eligible resting orders against that fresh book."""
    if not hasattr(broker, "step"):
        return
    try:
        feed.refresh(clock())
    except Exception:
        log.exception("bot: pre-tick feed refresh failed")
    try:
        broker.step(clock())
    except Exception:
        log.exception("bot: broker step failed")


def build_stack(store: Store, broker, rules: Mapping[str, SymbolRules], config: Config, clock):
    """(KillSwitch, RiskEngine, OMS) with the autonomous risk-gated approval policy."""
    ks = KillSwitch(store)
    risk = RiskEngine(config, rules, ks)
    oms = OMS(store, broker, risk, ks, approve=auto_approve, clock=clock)
    return ks, risk, oms


def build_bot(feed, oms, risk, store, ks, config: Config, clock, *, advisor: Any = None,
              params: BotParams | None = None, exit_policy: str = "E4", profile: str = "strict",
              run_info: Mapping[str, Any] | None = None) -> Bot:
    """S1 momentum entries (profile strict, or the demo EXERCISE profile), E4 hybrid exits (fee rate
    from config), risk-based sizing."""
    info = {"mode": "mock", **dict(run_info or {})}
    engine = MomentumSignalEngine(momentum_params(profile, str(info["mode"]), config.mode))
    policy = make_policy(exit_policy, ExitParams(fee_rate=config.taker_rate))
    return Bot(feed, engine, policy, size_position, oms, risk, store, ks, config, clock, advisor,
               params=params, profile=profile, run_info=info)


def make_guardian(enabled: bool, db: str | Path, broker, rules: Mapping[str, SymbolRules], config: Config,
                  clock, sleep, interval_s: float, universe, *, monotonic=None,
                  stale_action: str = "hold") -> InProcessGuardian | None:
    """`monotonic`: None = time.monotonic (real runs); a simulated / injected clock passes itself."""
    if not enabled:
        return None
    try:
        return InProcessGuardian(db, broker, rules, config, GuardianConfig(stale_action=stale_action), clock=clock,
                                 sleep=sleep, interval_s=interval_s, universe=universe, monotonic=monotonic)
    except ValueError as e:
        raise BotSetupError(f"guardian: {e} (or pass --no-guardian)") from e


def _run_info(mode: str, profile: str, guardian: bool, guardian_interval_s: float, stale_action: str = "hold",
              **extra: Any) -> dict[str, Any]:
    return {"mode": mode, "profile": profile,
            "guardian": {"enabled": bool(guardian), "in_process": True, "interval_s": guardian_interval_s,
                         "stale_action": stale_action},
            **extra}


def max_interval_s(config: Config) -> float:
    """Longest allowed wall-clock --interval-s for the live-feed loops: max_data_age_s / 2 (15 s)."""
    return float(config.max_data_age_s) / 2


def interval_problem(interval_s: float, config: Config) -> str | None:
    """Why a live-feed (mock / shadow without replay) tick interval is refused, or None.

    The feed (and so every quote the bot and the guardian see) refreshes only on bot ticks, and a
    quote older than max_data_age_s (30 s) is stale. With interval_s <= max_data_age_s / 2 the
    oldest quote the guardian can see on any of its ticks, including the wake-ups while the bot
    sleeps, is under interval_s + the feed's per-symbol poll interval (12 s) <= 27 s: never stale
    on a healthy feed."""
    limit = max_interval_s(config)
    if not 0 < interval_s <= limit:
        return (f"--interval-s {interval_s:g} is outside (0, {limit:g}] s: the market data refreshes only on bot "
                f"ticks and quotes older than max_data_age_s ({float(config.max_data_age_s):g} s) count as stale, so "
                f"the interval may be at most max_data_age_s / 2")
    return None


def _guarded_sleep(sleep: Callable[[float], Any], total: float, guardian: InProcessGuardian | None,
                   stop: StopFlag) -> None:
    """Sleep `total` seconds; with a guardian, in chunks of its interval, ticking it in between."""
    if guardian is None:
        sleep(total)
        return
    remaining = total
    while remaining > 0 and not stop.is_set():
        chunk = min(remaining, guardian.interval_s)
        sleep(chunk)
        remaining -= chunk
        if remaining > 0 and not stop.is_set() and guardian.due():
            guardian.tick()


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


def summarize(store: Store, bot: Bot, ticks: int, guardian: InProcessGuardian | None = None) -> dict[str, Any]:
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
            "consecutive_failures": bot.consecutive_failures, "profile": bot.profile,
            "feed_down_since": bot.feed_down_since, "feed_outage_s": bot.feed_outage_s,
            "guardian": None if guardian is None else {"ticks": guardian.ticks, "failed": guardian.failed_ticks}}


# ---------------------------------------------------------------------------- runners
def run_replay(db: str | Path, replay_dir: str | Path, config: Config, *, step_s: float = 60.0,
               max_ticks: int | None = None, balances: Mapping[str, Decimal] | None = None,
               advisor: Any = None, params: BotParams | None = None, stop: StopFlag | None = None,
               symbols: Sequence[str] | None = None, on_tick: Callable[[Any], Any] | None = None,
               profile: str = "strict", guardian: bool = True,
               guardian_interval_s: float = GUARDIAN_INTERVAL_S, stale_action: str = "hold") -> dict[str, Any]:
    """Offline end-to-end paper run over recorded 1m candles under an accelerated clock. The guardian
    ticks after every bot tick (simulated time; flatten waits advance the simulated clock)."""
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
    gd = None
    try:
        ks, risk, oms = build_stack(store, broker, rules, config, clock)
        bot = build_bot(feed, oms, risk, store, ks, config, clock, advisor=advisor, params=params, profile=profile,
                        run_info=_run_info("mock", profile, guardian, guardian_interval_s, stale_action, replay=True))
        gd = make_guardian(guardian, db, broker, rules, config, clock, clock.advance, guardian_interval_s,
                           lambda: set(feed.symbols()) | set(bot.positions), monotonic=clock,
                           stale_action=stale_action)
        ticks = 0
        while clock() <= last + MIN_S and not stop.is_set() and (max_ticks is None or ticks < max_ticks):
            rep = bot.tick()
            if gd is not None:
                gd.tick()
            ticks += 1
            if on_tick is not None:
                on_tick(rep)
            clock.advance(step_s)
        return summarize(store, bot, ticks, gd)
    finally:
        if gd is not None:
            gd.close()
        store.close()


def run_feed_loop(mode: str, db: str | Path, config: Config, *, interval_s: float = 12.0,
                  max_ticks: int | None = None, client: Any = None, balances: Mapping[str, Decimal] | None = None,
                  advisor: Any = None, stop: StopFlag | None = None, clock: Callable[[], float] = time.time,
                  sleep: Callable[[float], Any] | None = None,
                  on_tick: Callable[[Any], Any] | None = None,
                  journal_path: str | Path | None = None, profile: str = "strict", guardian: bool = True,
                  guardian_interval_s: float = GUARDIAN_INTERVAL_S, stale_action: str = "hold") -> dict[str, Any]:
    """mock (paper broker mirrored from the public book) or shadow (ShadowBroker) on PublicFeed. The
    guardian ticks after every bot tick and, during longer sleeps, every guardian_interval_s."""
    problem = interval_problem(interval_s, config)
    if problem is not None:
        raise BotSetupError(problem)
    stop = stop or StopFlag()
    sleep = sleep or stop.wait
    if client is None:
        from wallexbot.exchange.wallex_public import WallexPublicClient
        client = WallexPublicClient()
    rules = {r.symbol: r for r in client.markets() if r.quote == config.quote_currency}
    feed = PublicFeed(client, config)
    broker = make_broker(mode, config=config, clock=clock, rules=rules, balances=balances, journal_path=journal_path,
                         book_source=fresh_depth_source(feed, clock))
    if mode == "mock":
        feed.on_book = broker.set_book   # paper fills at the live public prices
    store = Store(db, clock=clock)
    gd = None
    try:
        ks, risk, oms = build_stack(store, broker, rules, config, clock)
        bot = build_bot(feed, oms, risk, store, ks, config, clock, advisor=advisor, profile=profile,
                        run_info=_run_info(mode, profile, guardian, guardian_interval_s, stale_action))
        gd = make_guardian(guardian, db, broker, rules, config, clock, sleep, guardian_interval_s,
                           lambda: set(feed.symbols()) | set(bot.positions),
                           monotonic=None if clock is time.time else clock, stale_action=stale_action)
        ticks = 0
        while not stop.is_set() and (max_ticks is None or ticks < max_ticks):
            pre_tick(feed, broker, clock)
            rep = bot.tick()
            if gd is not None:
                gd.tick()
            ticks += 1
            if on_tick is not None:
                on_tick(rep)
            if stop.is_set() or (max_ticks is not None and ticks >= max_ticks):
                break
            _guarded_sleep(sleep, interval_s, gd, stop)
        return summarize(store, bot, ticks, gd)
    finally:
        if gd is not None:
            gd.close()
        store.close()


# ---------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None, *, advisor: Any = None,
         after_tick: Callable[[Any], Any] | None = None) -> int:
    """`advisor` / `after_tick` are injected by the top-level CLI (bot/ itself never imports ai/)."""
    p = argparse.ArgumentParser(prog="python -m wallexbot.bot.main",
                                description="Autonomous trading loop (mock / shadow only; live is refused).")
    p.add_argument("--db", required=True, help="path of the audit SQLite database")
    p.add_argument("--mode", required=True, help="mock | shadow (live is refused)")
    p.add_argument("--replay-from", default=None, help="data.history directory for an offline replay (mock)")
    p.add_argument("--max-ticks", type=int, default=None, help="stop after N ticks")
    p.add_argument("--interval-s", type=float, default=None,
                   help="replay: simulated seconds per tick (60); otherwise wall seconds between ticks (12)")
    p.add_argument("--config", default=None, help="config YAML (default: $WALLEXBOT_CONFIG, else the packaged default_config.yaml)")
    p.add_argument("--balance", default=str(DEFAULT_BALANCE), help="starting balance in the quote currency (USDT)")
    p.add_argument("--profile", default="strict", choices=PROFILES,
                   help="strict (default: S1 as specified) | demo (EXERCISE profile, relaxed thresholds, "
                        "mock/shadow only, no edge claimed)")
    p.add_argument("--no-guardian", action="store_true",
                   help="do not run the in-process guardian (exercises the bot alone; no watchdog)")
    p.add_argument("--guardian-interval-s", type=float, default=GUARDIAN_INTERVAL_S,
                   help="guardian ticks after every bot tick and at least this often while the bot sleeps (15)")
    p.add_argument("--stale-action", choices=STALE_ACTIONS, default="hold",
                   help="guardian, held position with no fresh quote for > 120 s: hold (default; alert every "
                        "5 min) or flatten (reduce-only exits)")
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
        cfg = load_config(args.config, env={})   # never armed: no live path from here
        balance = Decimal(args.balance)
    except (ConfigError, OSError, ArithmeticError, ValueError, KeyError) as e:
        print(f"refused: bad configuration: {e}", file=sys.stderr)
        return 2
    try:
        momentum_params(args.profile, args.mode, cfg.mode)
    except ProfileError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if not 0 < args.guardian_interval_s <= 3600:
        print("refused: --guardian-interval-s must be in (0, 3600]", file=sys.stderr)
        return 2
    if args.replay_from:
        if args.interval_s is not None and not 0 < args.interval_s <= BAR_S:
            print(f"refused: replay --interval-s (simulated seconds per tick) must be in (0, {BAR_S}]",
                  file=sys.stderr)
            return 2
    else:
        problem = interval_problem(args.interval_s if args.interval_s is not None else 12.0, cfg)
        if problem is not None:
            print(f"refused: {problem}", file=sys.stderr)
            return 2
    guardian = not args.no_guardian
    if guardian and str(args.db) == ":memory:":
        print("refused: the in-process guardian needs a database file; use a path or --no-guardian",
              file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.profile == "demo":
        print(f"*** {EXERCISE_BANNER} ***", flush=True)
    print(f"profile: {args.profile}", flush=True)
    print(GUARDIAN_ON.format(interval=args.guardian_interval_s) if guardian else GUARDIAN_OFF, flush=True)
    stop = StopFlag()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())

    def show(rep) -> None:
        if after_tick is not None:
            after_tick(rep)
        if rep.actions or rep.errors:          # feed outages are logged every 5 min by the bot itself
            down = "" if rep.feed_down_since is None else f" feed_down_s={rep.feed_outage_s or 0:.0f}"
            print(f"tick ts={rep.ts:.0f} actions={rep.actions} errors={rep.errors}{down}", flush=True)

    try:
        balances = {cfg.quote_currency: balance}
        if args.replay_from:
            summary = run_replay(args.db, args.replay_from, cfg, step_s=args.interval_s or 60.0,
                                 max_ticks=args.max_ticks, balances=balances, stop=stop, on_tick=show,
                                 advisor=advisor, profile=args.profile, guardian=guardian,
                                 guardian_interval_s=args.guardian_interval_s, stale_action=args.stale_action)
        else:
            journal = None if str(args.db) == ":memory:" else Path(args.db).parent / JOURNAL_NAME
            summary = run_feed_loop(args.mode, args.db, cfg, interval_s=args.interval_s or 12.0,
                                    max_ticks=args.max_ticks, balances=balances, stop=stop, on_tick=show,
                                    advisor=advisor, journal_path=journal if args.mode == "shadow" else None,
                                    profile=args.profile, guardian=guardian,
                                    guardian_interval_s=args.guardian_interval_s, stale_action=args.stale_action)
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
