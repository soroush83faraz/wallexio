"""CLI back end for `python -m wallexbot record` and `python -m wallexbot history`.

Public market data only: every request is an unauthenticated GET through `WallexPublicClient`, which is pinned to
api.wallex.ir and has no non-GET method. Nothing here imports a broker, an OMS or the risk engine, so neither
command can sit on an order path, and no credential is read. The client is injectable for tests.
"""
from __future__ import annotations

import logging
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

# The same seven markets as the backtest grid (backtest.runner.DEFAULT_SYMBOLS); kept here so this module stays light.
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "XAUTUSDT", "USOONUSDT", "PEPEUSDT", "SOLUSDT")
_SYMBOL = re.compile(r"[A-Z0-9]{3,20}")
DAY_S = 86_400


class UsageError(ValueError):
    """Bad command line: reported as `refused: ...`, exit code 2."""


def parse_symbols(raw: list[str] | None) -> list[str]:
    """`--symbols BTCUSDT,ETHUSDT` or `--symbols BTCUSDT ETHUSDT`; order kept, duplicates dropped."""
    items = [s for chunk in (raw or []) for s in re.split(r"[,\s]+", chunk) if s]
    bad = [s for s in items if not _SYMBOL.fullmatch(s)]
    if bad:
        raise UsageError(f"bad symbol(s): {bad}")
    return list(dict.fromkeys(items))


def resolve_symbols(client, symbols: list[str] | None, top_n: int | None) -> list[str]:
    """--symbols, else the top-N USDT markets by 24h volume (one public GET), else the default seven."""
    if symbols and top_n is not None:
        raise UsageError("use --symbols or --top-n, not both")
    if symbols:
        out = parse_symbols(symbols)
        if not out:
            raise UsageError("--symbols is empty")
        return out
    if top_n is None:
        return list(DEFAULT_SYMBOLS)
    if not 1 <= top_n <= 100:
        raise UsageError("--top-n must be between 1 and 100")
    from wallexbot.config import load_config
    quote = load_config(env={}).quote_currency
    ranked = [r.symbol for r in client.rank_universe(quote, top_n)]
    if not ranked:
        raise RuntimeError(f"no {quote} markets returned by the exchange")
    return ranked


def _public_client(client):
    if client is not None:
        return client
    from wallexbot.exchange.wallex_public import WallexPublicClient
    return WallexPublicClient()


@contextmanager
def _signals(on_signal: Callable[[int], None]) -> Iterator[None]:
    """SIGTERM/SIGINT -> on_signal, restoring the previous handlers afterwards (tests, embedding)."""
    sigs = (signal.SIGINT, signal.SIGTERM)
    try:
        old = {s: signal.signal(s, lambda n, _f: on_signal(n)) for s in sigs}
    except ValueError:            # not the main thread: nothing to install
        yield
        return
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def run_record(args, *, client=None) -> int:
    """Recorder loop; SIGTERM/SIGINT finish the current cycle, then exit 0. The handlers are installed before
    anything else: as PID 1 in a container an early SIGTERM would otherwise be ignored (-> SIGKILL after the grace)."""
    stop = threading.Event()
    holder: list = []

    def on_signal(_n: int) -> None:
        stop.set()
        for rec in holder:
            rec.stop()

    with _signals(on_signal):
        try:
            if args.interval_s < 1:
                raise UsageError("--interval-s must be >= 1")
            if args.max_cycles is not None and args.max_cycles < 1:
                raise UsageError("--max-cycles must be >= 1")
            client = _public_client(client)
            symbols = resolve_symbols(client, args.symbols, args.top_n)
        except UsageError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        except Exception as e:  # noqa: BLE001 - network/universe failure before the first cycle
            print(f"record: cannot resolve the symbol universe: {e}", file=sys.stderr)
            return 1
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        from wallexbot.data.recorder import Recorder
        rec = Recorder(client, symbols, args.out, interval_s=args.interval_s)
        holder.append(rec)
        if stop.is_set():
            rec.stop()
        print(f"recorder: {len(symbols)} symbols {','.join(symbols)} -> {args.out} every {args.interval_s:g}s",
              flush=True)
        rec.run_forever(handle_signals=False, max_cycles=args.max_cycles)
    print(f"recorder stopped: {rec.stats}", flush=True)
    return 0


def run_history(args, *, client=None, clock: Callable[[], float] = time.time,
                sleep: Callable[[float], None] = time.sleep) -> int:
    """Resumable 1m download of the last --days days, newest first; re-running only fetches what is missing.
    Exit 0 = every symbol finished; 1 = stopped early (budget, repeated failures, signal) or a symbol failed:
    progress is on disk, run the same command again."""
    from wallexbot.data.history import HistoryError, RequestBudget, fetch_history

    def _interrupt(_n: int) -> None:
        raise KeyboardInterrupt          # unwinds through fetch_history's `finally`, which checkpoints to disk

    rc = 0
    budget = RequestBudget(max(args.max_requests, 1))
    try:
        with _signals(_interrupt):       # installed first: as PID 1 an early SIGTERM would otherwise be ignored
            try:
                if not 1 <= args.days <= 3650:
                    raise UsageError("--days must be between 1 and 3650")
                if args.max_requests < 1:
                    raise UsageError("--max-requests must be >= 1")
                client = _public_client(client)
                symbols = resolve_symbols(client, args.symbols, args.top_n)
            except UsageError as e:
                print(f"refused: {e}", file=sys.stderr)
                return 2
            except Exception as e:  # noqa: BLE001
                print(f"history: cannot resolve the symbol universe: {e}", file=sys.stderr)
                return 1
            end = int(clock())
            start = end - args.days * DAY_S
            Path(args.out).mkdir(parents=True, exist_ok=True)
            print(f"history: {len(symbols)} symbols, last {args.days} d, budget {args.max_requests} requests "
                  f"-> {args.out}", flush=True)
            for sym in symbols:
                if budget.remaining <= 0:
                    print(f"  {sym}: skipped, request budget exhausted (re-run to resume)", flush=True)
                    rc = 1
                    continue
                try:
                    rep = fetch_history(client, sym, start, end, args.out, budget=budget, clock=clock, sleep=sleep)
                except (HistoryError, ValueError, OSError) as e:
                    print(f"  {sym}: FAILED: {e}", file=sys.stderr, flush=True)
                    rc = 1
                    continue
                print(f"  {sym}: rows={rep.rows} window_coverage={rep.window_coverage:.1%} gaps={rep.gaps} "
                      f"requests={rep.requests} reached_start={rep.reached_start}"
                      + (f" ABORTED: {rep.aborted}" if rep.aborted else ""), flush=True)
                if rep.aborted:
                    rc = 1
    except KeyboardInterrupt:
        print("history: interrupted; progress saved, re-run the same command to resume", file=sys.stderr, flush=True)
        return 1
    print(f"history done: {budget.used}/{budget.limit} requests used"
          + ("" if rc == 0 else " (incomplete: re-run to resume)"), flush=True)
    return rc
