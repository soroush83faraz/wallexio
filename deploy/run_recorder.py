#!/usr/bin/env python3
"""Market-data recorder entry point (public GET requests only; no credentials, no orders).

Appends depth and trades to <out-dir>/depth_<YYYYMMDD>.jsonl and trades_<YYYYMMDD>.jsonl using
wallexbot.data.recorder.Recorder. SIGTERM / SIGINT stop it after the current cycle.

    python deploy/run_recorder.py --out-dir /data/recorder [--symbols BTCUSDT ETHUSDT] [--interval-s 15]

Symbols: --symbols, else the space/comma separated env WALLEXBOT_RECORDER_SYMBOLS, else DEFAULT_SYMBOLS
(the same seven markets as the backtest grid).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "XAUTUSDT", "USOONUSDT", "PEPEUSDT", "SOLUSDT")
_SYMBOL = re.compile(r"[A-Z0-9]{3,20}")


def resolve_symbols(cli: list[str] | None, env: str | None) -> list[str]:
    raw = cli or [s for s in re.split(r"[,\s]+", env or "") if s] or list(DEFAULT_SYMBOLS)
    bad = [s for s in raw if not _SYMBOL.fullmatch(s)]
    if bad:
        raise ValueError(f"bad symbol(s): {bad}")
    return list(dict.fromkeys(raw))


def main(argv: list[str] | None = None, *, client=None) -> int:
    p = argparse.ArgumentParser(description="wallexbot market-data recorder (public GETs only)")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--interval-s", type=float, default=15.0)
    p.add_argument("--max-cycles", type=int, default=None, help="stop after N cycles (default: until signal)")
    args = p.parse_args(argv)
    try:
        symbols = resolve_symbols(args.symbols, os.environ.get("WALLEXBOT_RECORDER_SYMBOLS"))
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if args.interval_s < 1:
        print("refused: --interval-s must be >= 1", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from wallexbot.data.recorder import Recorder
    if client is None:
        from wallexbot.exchange.wallex_public import WallexPublicClient
        client = WallexPublicClient()
    rec = Recorder(client, symbols, args.out_dir, interval_s=args.interval_s)
    rec.run_forever(handle_signals=args.max_cycles is None, max_cycles=args.max_cycles)
    print(f"recorder stopped: {rec.stats}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
