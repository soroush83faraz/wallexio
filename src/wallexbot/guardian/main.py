"""Guardian runners: its own SQLite connection, no strategy code.

In this build (mock/shadow) the guardian that protects a bot runs INSIDE the bot process:
InProcessGuardian opens its own Store connection on the bot's database, builds its own
KillSwitch / RiskEngine / OMS around the bot's broker OBJECT (the paper MockWallex or the
ShadowBroker), and is ticked by the bot runner after every bot tick (bot/main.py). A failed tick is
logged (errors table + logger); `max_consecutive_failures` failed ticks in a row engage the kill
switch for entries. A separate guardian PROCESS only makes sense against a real exchange account
(live), which this build does not have: a separate process cannot see an in-process paper broker.

run_guardian() is the standalone loop. Its CLI is a DEMO only (no network, no credentials):
    python -m wallexbot.guardian.main --db NEW.db --mode mock --demo-db [--ticks N] [--interval S]
It wires its OWN in-memory MockWallex (10000 USDT); --mode shadow is the same mock with flatten
dry-run. It refuses to run without --demo-db, and on any database that holds bot data (audit
events by 'bot'/'strategy') or equity / audit rows it did not write itself: its equity rows
(start-of-day, high-water mark) would corrupt a bot's drawdown and balance checks.

Run exactly ONE guardian per database: recover_guardian_intents() at startup assumes that the
guardian's own half-submitted intents belong to a dead predecessor.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote as _urlquote

from wallexbot.config import Config, load_config
from wallexbot.guardian.flatten import ACTOR, FlattenConfig, recover_guardian_intents
from wallexbot.guardian.monitor import Guardian, GuardianConfig, TickReport
from wallexbot.oms import OMS
from wallexbot.risk.engine import RiskEngine
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import Store
from wallexbot.types import SymbolRules

ALLOWED_MODES = ("mock", "shadow")
DEMO_MARKER_KIND = "demo_db"           # audit_events(actor='guardian', kind='demo_db'): a demo-guardian DB
BOT_ACTORS = ("bot", "strategy")       # the bot's decision log and heartbeat
DEFAULT_INTERVAL_S = 15.0
log = logging.getLogger("wallexbot.guardian")


class StopFlag:
    """Graceful stop: set() from a signal handler or another thread; wait() is an interruptible sleep."""

    def __init__(self) -> None:
        self._ev = threading.Event()

    def set(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def wait(self, seconds: float) -> None:
        self._ev.wait(max(seconds, 0.0))


@dataclass
class RunSummary:
    ticks: int = 0
    failed_ticks: int = 0
    consecutive_failures: int = 0
    engaged_for_failures: bool = False
    last_report: TickReport | None = None


def build_guardian(store: Store, broker, rules: Mapping[str, SymbolRules], config: Config,
                   gconfig: GuardianConfig | None = None, *, clock: Callable[[], float] = time.time,
                   sleep: Callable[[float], Any] = time.sleep,
                   depth_fn: Callable[[str], Sequence] | None = None,
                   universe: Callable[[], Iterable[str]] | None = None) -> Guardian:
    ks = KillSwitch(store)
    risk = RiskEngine(config, rules, ks)
    oms = OMS(store, broker, risk, ks, clock=clock)  # default approval: risk-approved only
    return Guardian(store, broker, risk, ks, oms, gconfig, clock, sleep=sleep, depth_fn=depth_fn,
                    universe=universe)


class InProcessGuardian:
    """The guardian inside the bot process (mock/shadow): the SAME broker object as the bot, its
    OWN Store connection on the bot's database. tick() never raises; it writes a 'guardian'
    heartbeat, then runs Guardian.tick(). The runner calls tick() after every bot tick and, when
    the bot sleeps longer than `interval_s`, in between (due())."""

    def __init__(self, db_path: str | Path, broker, rules: Mapping[str, SymbolRules], config: Config,
                 gconfig: GuardianConfig | None = None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = time.sleep, interval_s: float = DEFAULT_INTERVAL_S,
                 universe: Callable[[], Iterable[str]] | None = None, max_consecutive_failures: int = 3) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("the in-process guardian needs a database file (a second :memory: connection "
                             "is a different, empty database)")
        if not 0 < interval_s <= 3600:
            raise ValueError("guardian interval_s must be in (0, 3600]")
        self.clock, self.interval_s, self.max_failures = clock, float(interval_s), max_consecutive_failures
        self.store = Store(db_path, clock=clock)
        self.guardian = build_guardian(self.store, broker, rules, config, gconfig, clock=clock, sleep=sleep,
                                       universe=universe)
        self.ticks = self.failed_ticks = self.consecutive_failures = 0
        self.last_report: TickReport | None = None
        self._last: float | None = None
        try:
            recover_guardian_intents(self.store, float(clock()))
        except Exception as e:
            log.exception("guardian: startup recovery failed")
            _safe_error(self.guardian, f"startup recovery failed: {e!r}")

    def due(self) -> bool:
        """interval_s elapsed since the last tick; a backward clock step makes it due (re-anchor)."""
        if self._last is None:
            return True
        elapsed = float(self.clock()) - self._last
        return not 0 <= elapsed < self.interval_s

    def tick(self) -> TickReport | None:
        now = float(self.clock())
        self._last = now
        self.ticks += 1
        try:
            self.store.heartbeat(ACTOR, ts=now)
            self.last_report = self.guardian.tick()
            self.consecutive_failures = 0
            return self.last_report
        except Exception as e:  # never take the bot down
            self.failed_ticks += 1
            self.consecutive_failures += 1
            log.exception("guardian: tick failed (%d in a row)", self.consecutive_failures)
            _safe_error(self.guardian, f"tick failed: {e!r}")
            if self.consecutive_failures >= self.max_failures:
                _engage_for_failures(self.guardian, self.consecutive_failures)
            return None

    def close(self) -> None:
        self.store.close()


def demo_db_problem(db_path: str | Path) -> str | None:
    """Why the standalone DEMO guardian must not use this database (None: new, empty, or a
    previous demo-guardian database). Read-only; never creates the file."""
    if str(db_path) == ":memory:":
        return None
    p = Path(db_path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        conn = sqlite3.connect("file:" + _urlquote(str(p.absolute())) + "?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error as e:
        return f"it cannot be opened read-only ({type(e).__name__})"
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return None
        if "audit_events" not in tables:
            return "it is not an empty database and not a wallexbot audit database"

        def count(sql: str, args: tuple = ()) -> int:
            return int(conn.execute(sql, args).fetchone()[0])
        marks = ",".join("?" for _ in BOT_ACTORS)
        n_bot = count(f"SELECT COUNT(*) FROM audit_events WHERE actor IN ({marks})", BOT_ACTORS)
        if n_bot:
            return (f"it holds {n_bot} bot audit event(s) (actor {' / '.join(BOT_ACTORS)}): a bot uses this "
                    "database and runs its own guardian in-process")
        if count("SELECT COUNT(*) FROM audit_events WHERE actor='guardian' AND kind=?", (DEMO_MARKER_KIND,)):
            return None
        rows = sum(count(f"SELECT COUNT(*) FROM {t}") for t in sorted(tables)
                   if t in ("equity_sod", "equity_hwm", "audit_events", "order_intents", "signals", "fills",
                            "killswitch_events", "positions_snapshots"))
        if rows:
            return f"it holds {rows} equity / audit row(s) not written by the demo guardian"
        return None
    except sqlite3.Error as e:
        return f"it cannot be inspected ({type(e).__name__}: {e})"
    finally:
        conn.close()


def run_loop(guardian: Guardian, *, interval_s: float = 5.0, sleep: Callable[[float], Any] | None = None,
             stop: StopFlag | None = None, max_ticks: int | None = None, max_consecutive_failures: int = 3,
             before_tick: Callable[[], Any] | None = None,
             on_tick: Callable[[TickReport], Any] | None = None) -> RunSummary:
    stop = stop or StopFlag()
    sleep = sleep or stop.wait
    out = RunSummary()
    try:
        recover_guardian_intents(guardian.store, float(guardian.clock()))
    except Exception as e:
        log.exception("guardian: startup recovery failed")
        _safe_error(guardian, f"startup recovery failed: {e!r}")
    while not stop.is_set() and (max_ticks is None or out.ticks < max_ticks):
        try:
            if before_tick is not None:
                before_tick()
            out.last_report = guardian.tick()
            out.consecutive_failures = 0
            if on_tick is not None:
                on_tick(out.last_report)
        except Exception as e:  # never crash on one bad tick
            out.failed_ticks += 1
            out.consecutive_failures += 1
            log.exception("guardian: tick failed (%d in a row)", out.consecutive_failures)
            _safe_error(guardian, f"tick failed: {e!r}")
            if out.consecutive_failures >= max_consecutive_failures:
                out.engaged_for_failures |= _engage_for_failures(guardian, out.consecutive_failures)
        out.ticks += 1
        if stop.is_set() or (max_ticks is not None and out.ticks >= max_ticks):
            break
        sleep(interval_s)
    return out


def run_guardian(db_path: str | Path, broker, rules: Mapping[str, SymbolRules], config: Config,
                 gconfig: GuardianConfig | None = None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] | None = None, stop: StopFlag | None = None,
                 max_ticks: int | None = None, interval_s: float = 5.0, max_consecutive_failures: int = 3,
                 depth_fn: Callable[[str], Sequence] | None = None,
                 before_tick: Callable[[], Any] | None = None,
                 on_tick: Callable[[TickReport], Any] | None = None) -> RunSummary:
    """Open the guardian's OWN Store connection on db_path and run the loop until stopped."""
    stop = stop or StopFlag()
    sleep = sleep or stop.wait
    store = Store(db_path, clock=clock)
    try:
        g = build_guardian(store, broker, rules, config, gconfig, clock=clock, sleep=sleep, depth_fn=depth_fn)
        return run_loop(g, interval_s=interval_s, sleep=sleep, stop=stop, max_ticks=max_ticks,
                        max_consecutive_failures=max_consecutive_failures, before_tick=before_tick,
                        on_tick=on_tick)
    finally:
        store.close()


def _engage_for_failures(g: Guardian, n: int) -> bool:
    try:
        now = float(g.clock())
        # not transient: supersedes an auto engagement; read errors count as held (fail closed)
        if not g.ks.engage_unless_held(f"guardian: {n} consecutive tick failures", actor=ACTOR, ts=now):
            return False
        g.store.append_audit_event(ACTOR, "engage_kill_switch", {"reasons": [f"tick_failures:{n}"]}, ts=now)
        return True
    except Exception:
        log.exception("guardian: could not engage the kill switch after %d failed ticks", n)
        return False


def _safe_error(g: Guardian, message: str) -> None:
    try:
        g.store.append_error("guardian.loop", message, ts=float(g.clock()))
    except Exception:
        log.exception("guardian: could not record a loop error")


# ---------------------------------------------------------------------------- CLI (demo only)
DEMO_ONLY = ("the standalone guardian is a DEMO: it watches its OWN in-memory mock broker (10000 USDT), not a "
             "bot's broker, and writes start-of-day / high-water-mark equity rows. On a bot's database that "
             "corrupts drawdown and triggers false balance-mismatch kill switches. A bot started with "
             "'wallexbot bot --mode mock|shadow' already runs the guardian in-process on its own broker "
             "(--no-guardian disables it). A separate guardian process only makes sense against a live "
             "exchange, which this build does not have.")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wallexbot.guardian.main",
                                description="Guardian DEMO loop on its own mock broker (no network). "
                                            "Needs --demo-db and a new/empty (or demo) database.")
    p.add_argument("--db", required=True, help="path of a NEW audit SQLite database for the demo")
    p.add_argument("--mode", required=True, help="mock | shadow (anything else is refused)")
    p.add_argument("--demo-db", action="store_true",
                   help="confirm that --db is a throwaway demo database (refused if it holds bot data)")
    p.add_argument("--ticks", type=int, default=None, help="stop after N ticks (default: until signal)")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between ticks")
    args = p.parse_args(argv)
    if args.mode not in ALLOWED_MODES:
        print(f"refused: guardian mode {args.mode!r} is not allowed (only {', '.join(ALLOWED_MODES)})",
              file=sys.stderr)
        return 2
    if not args.demo_db:
        print(f"refused: {DEMO_ONLY} To run the demo, pass --demo-db with a new database path.", file=sys.stderr)
        return 2
    problem = demo_db_problem(args.db)
    if problem is not None:
        print(f"refused: {args.db}: {problem}. {DEMO_ONLY} Use a new database path for the demo.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(env={})  # never armed: this entry point cannot reach a live exchange
    from wallexbot.pipeline import build_mock_broker  # demo wiring lives on the order-path side
    clock = time.time
    broker, rules = build_mock_broker(cfg, clock)
    sym = next(iter(rules))
    gcfg = GuardianConfig(flatten=FlattenConfig(dry_run=args.mode == "shadow"))
    stop = StopFlag()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())

    def feed() -> None:  # simulated market data so the demo feed stays fresh
        broker.set_price(sym, Decimal("60000.00"), Decimal("60010.00"))

    def show(rep: TickReport) -> None:
        print(f"tick equity={rep.equity} conditions={rep.conditions} actions={rep.actions}", flush=True)

    try:
        marker = Store(args.db, clock=clock)
        try:
            if not marker.audit_events(ACTOR, DEMO_MARKER_KIND):
                marker.append_audit_event(ACTOR, DEMO_MARKER_KIND, {"broker": "in-memory MockWallex",
                                                                    "mode": args.mode})
        finally:
            marker.close()
        summary = run_guardian(args.db, broker, rules, cfg, gcfg, clock=clock, stop=stop, max_ticks=args.ticks,
                               interval_s=args.interval, before_tick=feed, on_tick=show)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"guardian stopped: ticks={summary.ticks} failed={summary.failed_ticks}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
