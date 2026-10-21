"""Command line entry: `python -m wallexbot <demo|backtest|doctor|guardian>`.

    demo       scripted mock round trip (signal -> risk -> OMS -> MockWallex), pretty-printed
    backtest   exploratory grid on train+validation of local 1m history (see backtest/runner.py);
               `--final-test --params-file frozen.json` is the single-use test-set evaluation
    doctor     read-only account preflight; needs WALLEX_API_KEY_READ in the environment (exit 2 without it)
    guardian   guardian demo loop, mock/shadow only

There is no live mode in this build: `--arm-live` is refused on every command.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Sequence

LIVE_REFUSAL = "refused: live is not available in this build (--arm-live does nothing; mock/shadow only)"
READ_KEY_ENV = "WALLEX_API_KEY_READ"
DEFAULT_HISTORY_DIR = Path("data") / "history"
DEFAULT_OUT_DIR = Path("reports")


def _demo(args: argparse.Namespace) -> int:
    from wallexbot.pipeline import run_scripted_trade
    out = run_scripted_trade(args.db)
    print("wallexbot demo: scripted mock round trip on BTCUSDT (no network, no real money)")
    for label, res in (("entry", out["entry"]), ("exit ", out["exit"])):
        print(f"  {label}  state={res.state.value:<10} sent_to_mock_broker={res.sent}  client_id={res.client_id}")
    print(f"  realized P&L: {out['realized_pnl']} USDT")
    print("  audit rows: " + ", ".join(f"{t}={len(rows)}" for t, rows in out["audit"].items()))
    return 0


def _backtest(args: argparse.Namespace) -> int:
    from wallexbot.backtest import runner
    from wallexbot.backtest.splits import TestSetAlreadyUsed
    symbols = tuple(args.symbols) if args.symbols else runner.DEFAULT_SYMBOLS
    started = time.time()
    if args.final_test:
        try:
            rep = runner.run_final_test(args.history_dir, args.out_dir, args.params_file,
                                        ledger=args.ledger, symbols=symbols)
        except runner.FrozenParamsError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        except TestSetAlreadyUsed as e:
            print(f"refused: the test set is single-use and {e}", file=sys.stderr)
            return 3
        m = rep["metrics"]
        print(f"final test done: net_return={m['net_return']:.4%} trades={m['trade_count']} "
              f"report={Path(args.out_dir) / 'backtest_final_test.md'}")
        return 0
    if args.params_file:
        print("refused: --params-file is only used together with --final-test", file=sys.stderr)
        return 2
    rep = runner.run_grid(args.history_dir, args.out_dir, symbols=symbols, workers=args.workers)
    print(runner.render_grid_markdown(rep))
    print(f"\nreports written to {Path(args.out_dir)} (backtest_grid.*, backtest_top_*.*); "
          f"runtime {time.time() - started:.0f}s; test segment NOT evaluated")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from wallexbot.doctor import run_doctor
    from wallexbot.redact import load_secret
    key = load_secret(READ_KEY_ENV)
    if key is None:
        print(f"doctor needs a read-only Wallex API key in the environment variable {READ_KEY_ENV}; "
              "it is not set, so nothing was checked and no request was sent.", file=sys.stderr)
        return 2
    report = run_doctor(key, None)  # None: the real HTTPS transport; GET requests only
    print(report.render())
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m wallexbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="scripted mock round trip")
    d.add_argument("--db", default=":memory:", help="audit SQLite path (default: in memory)")
    d.set_defaults(func=_demo)

    b = sub.add_parser("backtest", help="exploratory backtest grid on train+validation")
    b.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR, help="1m history (default data/history)")
    b.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="report directory (default reports)")
    b.add_argument("--symbols", nargs="+", help="override the default 7-symbol universe")
    b.add_argument("--workers", type=int, default=4, help="worker processes (results do not depend on it)")
    b.add_argument("--final-test", action="store_true",
                   help="single-use test-set evaluation; requires --params-file")
    b.add_argument("--params-file", type=Path, help="frozen.json for --final-test")
    b.add_argument("--ledger", type=Path, default=None, help="test-set ledger (default reports/test_set_ledger.jsonl)")
    b.set_defaults(func=_backtest)

    r = sub.add_parser("doctor", help=f"read-only preflight (needs {READ_KEY_ENV})")
    r.set_defaults(func=_doctor)

    sub.add_parser("guardian", add_help=False,
                   help="guardian demo loop (mock/shadow only); all further arguments go to "
                        "`python -m wallexbot.guardian.main` (--db PATH --mode mock|shadow [--ticks N] ...)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a == "--arm-live" or a.startswith("--arm-live=") for a in argv):
        print(LIVE_REFUSAL, file=sys.stderr)
        return 2
    if argv[:1] == ["guardian"]:  # its own parser owns the remaining options
        from wallexbot.guardian.main import main as guardian_main
        return guardian_main(argv[1:])
    args = build_parser().parse_args(argv)
    if args.command == "backtest" and args.ledger is None:
        from wallexbot.backtest.runner import LEDGER_DEFAULT
        args.ledger = LEDGER_DEFAULT
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
