"""Command line entry: `python -m wallexbot <demo|backtest|doctor|guardian|bot>`.

    demo       scripted mock round trip (signal -> risk -> OMS -> MockWallex), pretty-printed
    backtest   exploratory grid on train+validation of local 1m history (see backtest/runner.py);
               `--cost-source recorded|proxy`, `--bar-minutes 15 60`, `--report-prefix v2_`;
               `--final-test --params-file frozen.json` is the single-use test-set evaluation
    calibrate-costs  spread/slippage calibration from recorded order books -> reports/cost_calibration.json
    doctor     read-only account preflight; needs WALLEX_API_KEY_READ in the environment (exit 2 without it)
    guardian   standalone guardian DEMO on its own mock broker and a NEW database (--demo-db); it refuses a
               bot's database (the bot runs its guardian in-process)
    bot        autonomous loop, mock (paper; offline with --replay-from) or shadow (live public book, simulated
               fills), in-process guardian (--no-guardian to disable), `--profile strict|demo` (demo = EXERCISE
               profile, no edge claimed); optional `--advisor off|shadow` (shadow = log-only AI verdicts)

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
    from wallexbot.backtest.calibrate import CostCalibration
    cal_path = Path(args.calibration) if args.calibration else runner.CALIBRATION_DEFAULT
    source = args.cost_source or ("recorded" if cal_path.is_file() else "proxy")
    calibration = None
    if source == "recorded":
        if not cal_path.is_file():
            print(f"refused: --cost-source recorded needs a calibration file ({cal_path} not found; run "
                  "`python -m wallexbot calibrate-costs`)", file=sys.stderr)
            return 2
        calibration = CostCalibration.load(cal_path)
    reports = []
    for minutes in args.bar_minutes:
        rep = runner.run_grid(args.history_dir, args.out_dir, symbols=symbols, workers=args.workers,
                              bar_minutes=minutes, cost_source=source, calibration=calibration,
                              report_prefix=args.report_prefix, calibration_path=cal_path if calibration else None,
                              **({"sens_only": ()} if args.skip_sensitivity else {}))
        reports.append(rep)
        print(runner.render_grid_markdown(rep))
    if args.report_prefix:
        summary = Path(args.out_dir) / f"{args.report_prefix}summary.md"
        summary.write_text(runner.render_summary_markdown(reports, title=f"Backtest {args.report_prefix.rstrip('_')} "
                                                                         f"summary ({source} costs)"))
    print(f"\nreports written to {Path(args.out_dir)} (prefix {args.report_prefix!r}, bars {args.bar_minutes}, "
          f"cost source {source}); runtime {time.time() - started:.0f}s; test segment NOT evaluated")
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    """Copy the recorder's depth files to a temporary directory (the recorder may be writing), calibrate
    on the copies, write the JSON (+ a Markdown rendering next to it)."""
    import shutil
    import tempfile
    from wallexbot.backtest.calibrate import calibrate_costs, render_calibration_markdown
    from wallexbot.backtest.runner import DEFAULT_SYMBOLS
    src = sorted(Path(args.recorder_dir).glob("depth_*.jsonl"))
    if not src:
        print(f"no depth_*.jsonl files in {args.recorder_dir}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        copies = []
        for p in src:
            dst = Path(tmp) / p.name
            shutil.copyfile(p, dst)
            copies.append(dst)
        cal = calibrate_costs(copies, tuple(args.symbols) if args.symbols else DEFAULT_SYMBOLS,
                              tuple(args.notionals))
    out = cal.save(args.out)
    md = out.with_suffix(".md")
    md.write_text(render_calibration_markdown(cal))
    print(render_calibration_markdown(cal))
    print(f"calibration written to {out} and {md} (sha256 {cal.sha256})")
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


def _bot(argv: list[str]) -> int:
    """`bot` passes everything to wallexbot.bot.main except --advisor, which is wired here (bot/ never imports ai/)."""
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--advisor", choices=("off", "shadow"), default="off")
    pre.add_argument("--db")
    pre.add_argument("--mode")
    ns, rest = pre.parse_known_args(argv)
    if ns.mode == "live":
        print(LIVE_REFUSAL, file=sys.stderr)
        return 2
    from wallexbot.bot.main import main as bot_main
    forwarded = list(rest) + (["--db", ns.db] if ns.db else []) + (["--mode", ns.mode] if ns.mode else [])
    if ns.db is None or ns.mode is None or ns.advisor == "off":
        return bot_main(forwarded)          # missing options: the bot's own parser reports them
    from wallexbot.advisor_bridge import build_advisor
    bridge, recorder = build_advisor(ns.advisor, ns.db, warn=lambda m: print(f"warning: {m}", file=sys.stderr))
    try:
        return bot_main(forwarded, advisor=bridge, after_tick=recorder)
    finally:
        if recorder is not None:
            recorder.close()


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
    b.add_argument("--cost-source", choices=("recorded", "proxy"), default=None,
                   help="execution costs: 'recorded' (calibrated from order books; per symbol, proxy where no "
                        "calibration) or 'proxy' (volume proxy). Default: recorded when the calibration exists")
    b.add_argument("--calibration", type=Path, default=None,
                   help="cost calibration JSON (default reports/cost_calibration.json)")
    b.add_argument("--bar-minutes", type=int, nargs="+", choices=(15, 60), default=[15],
                   help="bar size(s) S0/S1 are evaluated on (15 and/or 60); 60 gives few bars and low power")
    b.add_argument("--report-prefix", default="v2_",
                   help="report file prefix (default v2_; files <prefix>backtest_grid_<bar>m.*, <prefix>summary.md)")
    b.add_argument("--skip-sensitivity", action="store_true", help="skip the +/-20%% sensitivity of the top S1 variant")
    b.set_defaults(func=_backtest)

    c = sub.add_parser("calibrate-costs", help="calibrate spread/slippage from recorded order books")
    c.add_argument("--recorder-dir", type=Path, default=Path("data") / "recorder",
                   help="directory with depth_YYYYMMDD.jsonl (default data/recorder); files are copied first")
    c.add_argument("--symbols", nargs="+", help="override the default 7-symbol universe")
    c.add_argument("--notionals", nargs="+", type=int, default=[250, 500, 1000, 2500], help="order sizes, quote")
    c.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR / "cost_calibration.json",
                   help="output JSON (default reports/cost_calibration.json; a .md rendering is written next to it)")
    c.set_defaults(func=_calibrate)

    r = sub.add_parser("doctor", help=f"read-only preflight (needs {READ_KEY_ENV})")
    r.set_defaults(func=_doctor)

    sub.add_parser("guardian", add_help=False,
                   help="standalone guardian DEMO (own mock broker, new DB only); all further arguments go to "
                        "`python -m wallexbot.guardian.main` (--db NEW.db --mode mock|shadow --demo-db [--ticks N])")
    sub.add_parser("bot", add_help=False,
                   help="autonomous loop, mock|shadow only (--db PATH --mode mock|shadow [--replay-from DIR] "
                        "[--max-ticks N] [--balance USDT] [--profile strict|demo] [--no-guardian] "
                        "[--advisor off|shadow]); live is refused")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a == "--arm-live" or a.startswith("--arm-live=") for a in argv):
        print(LIVE_REFUSAL, file=sys.stderr)
        return 2
    if argv[:1] == ["guardian"]:  # its own parser owns the remaining options
        from wallexbot.guardian.main import main as guardian_main
        return guardian_main(argv[1:])
    if argv[:1] == ["bot"]:
        if any(a in ("-h", "--help") for a in argv[1:]):
            print("python -m wallexbot bot ... [--advisor off|shadow]  (shadow: log-only AI verdicts; needs "
                  "ANTHROPIC_API_KEY, else NullAdvisor)\nremaining options are those of python -m wallexbot.bot.main:")
        return _bot(argv[1:])
    args = build_parser().parse_args(argv)
    if args.command == "backtest" and args.ledger is None:
        from wallexbot.backtest.runner import LEDGER_DEFAULT
        args.ledger = LEDGER_DEFAULT
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
