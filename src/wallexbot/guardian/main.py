"""Guardian process: its own SQLite connection, its own loop, no strategy code.

run_guardian() opens Store(db_path) itself (a separate connection to the same WAL database the
strategy loop writes), builds its own KillSwitch / RiskEngine / OMS around the injected broker,
and ticks until stopped. A failed tick is logged (errors table + logger) and the loop continues;
`max_consecutive_failures` failed ticks in a row engage the kill switch for entries.

Run exactly ONE guardian per database: recover_guardian_intents() at startup assumes that the
guardian's own half-submitted intents belong to a dead predecessor.

CLI (demonstration only; no network, no credentials):
    python -m wallexbot.guardian.main --db PATH --mode mock [--ticks N] [--interval S]
--mode mock wires an in-memory MockWallex; --mode shadow wires the same mock but never writes to
the broker (flatten only plans and audits). Any other mode is refused.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from wallexbot.config import Config, load_config
from wallexbot.guardian.flatten import ACTOR, FlattenConfig, recover_guardian_intents
from wallexbot.guardian.monitor import Guardian, GuardianConfig, TickReport
from wallexbot.oms import OMS
from wallexbot.risk.engine import RiskEngine
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import Store
from wallexbot.types import SymbolRules

ALLOWED_MODES = ("mock", "shadow")
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
                   depth_fn: Callable[[str], Sequence] | None = None) -> Guardian:
    ks = KillSwitch(store)
    risk = RiskEngine(config, rules, ks)
    oms = OMS(store, broker, risk, ks, clock=clock)  # default approval: risk-approved only
    return Guardian(store, broker, risk, ks, oms, gconfig, clock, sleep=sleep, depth_fn=depth_fn)


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
        if g.ks.is_engaged():  # read errors count as engaged (fail closed): nothing to add
            return False
        now = float(g.clock())
        g.ks.engage(f"guardian: {n} consecutive tick failures", actor=ACTOR, ts=now)
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
def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wallexbot.guardian.main",
                                description="Guardian demo loop (mock/shadow only; no network).")
    p.add_argument("--db", required=True, help="path of the audit SQLite database")
    p.add_argument("--mode", required=True, help="mock | shadow (anything else is refused)")
    p.add_argument("--ticks", type=int, default=None, help="stop after N ticks (default: until signal)")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between ticks")
    args = p.parse_args(argv)
    if args.mode not in ALLOWED_MODES:
        print(f"refused: guardian mode {args.mode!r} is not allowed (only {', '.join(ALLOWED_MODES)})",
              file=sys.stderr)
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
        summary = run_guardian(args.db, broker, rules, cfg, gcfg, clock=clock, stop=stop, max_ticks=args.ticks,
                               interval_s=args.interval, before_tick=feed, on_tick=show)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"guardian stopped: ticks={summary.ticks} failed={summary.failed_ticks}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
