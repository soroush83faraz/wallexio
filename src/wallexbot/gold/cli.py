"""`python -m wallexbot gold-fetch | gold-backtest | gold-shadow` (wired in wallexbot/cli.py)."""
from __future__ import annotations

import argparse
import time
from decimal import Decimal
from pathlib import Path

from wallexbot.gold import data as gdata

DEFAULT_RUN_DIR = Path("runs")   # git-ignored


def add_parsers(sub) -> None:
    f = sub.add_parser("gold-fetch", help="download the gold inputs into gold/data/ (public GETs: Binance klines, "
                                          "Wallex candles/depth/markets); changes the backtest inputs")
    f.add_argument("--data-dir", type=Path, default=gdata.DEFAULT_DATA_DIR)
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("gold-backtest", help="pre-registered gold strategy on the committed gold/data/ (offline, "
                                             "deterministic) -> gold/report.json + gold/REPORT.md")
    b.add_argument("--data-dir", type=Path, default=gdata.DEFAULT_DATA_DIR)
    b.add_argument("--out-dir", type=Path, default=Path("gold"))
    b.set_defaults(func=cmd_backtest)

    s = sub.add_parser("gold-shadow", help="gold strategy through the bot / OMS / risk / ShadowBroker path: offline "
                                           "replay (default) -> runs/ + gold/SHADOW.md, or --live on the Wallex "
                                           "public book (GET only, no orders)")
    s.add_argument("--data-dir", type=Path, default=gdata.DEFAULT_DATA_DIR)
    s.add_argument("--out-dir", type=Path, default=Path("gold"))
    s.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR, help="audit DB + journal (default runs/)")
    s.add_argument("--live", action="store_true", help="poll the live Wallex public book and log would-be orders")
    s.add_argument("--market", choices=gdata.GOLD_MARKETS, default="PAXGUSDT", help="--live: Wallex market")
    s.add_argument("--bars-source", choices=("binance", "wallex"), default="binance",
                   help="--live: 1h decision bars (default binance, the series the backtest used)")
    s.add_argument("--max-ticks", type=int, default=None, help="--live: stop after N polls")
    s.add_argument("--interval-s", type=float, default=12.0, help="--live: seconds between polls (default 12)")
    s.set_defaults(func=cmd_shadow)


def cmd_fetch(args: argparse.Namespace) -> int:
    meta = gdata.fetch_all(args.data_dir)
    print(f"gold-fetch done ({meta['fetched_at_utc']}): files in {args.data_dir}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from wallexbot.gold.backtest import build_report, write_report
    t0 = time.time()
    try:
        rep = build_report(args.data_dir)
    except FileNotFoundError as e:
        print(f"error: {e}")
        return 2
    jp, mp = write_report(rep, args.out_dir)
    v0 = rep["variants"]["V0"]
    bh = rep["buy_and_hold"]["100pct"]
    print(f"gold-backtest: V0 total {v0['total_return']:+.2%}, geo avg {v0['geo_avg_monthly_return']:+.2%}/month, "
          f"max DD {v0['max_drawdown']:.2%}, trades {v0['trades']}; buy&hold {bh['total_return']:+.2%}; "
          f"3%/month target {'MET' if v0['target']['met'] else 'NOT MET'} ({time.time() - t0:.0f}s)")
    print(f"wrote {jp} and {mp}")
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    if args.live:
        from wallexbot.gold import shadow as gs
        name = f"gold_live_{args.market}"
        out = gs.run_live(args.run_dir / f"{name}.db", args.run_dir / f"{name}_journal.jsonl", symbol=args.market,
                          bars_source=args.bars_source, max_ticks=args.max_ticks, interval_s=args.interval_s,
                          balance=Decimal(1000))
        print(f"gold-shadow --live stopped after {out['ticks']} ticks: equity {out['equity_at_bid']:.2f} USDT at bid, "
              f"intents {out['intents_by_state']}; journal {args.run_dir / (name + '_journal.jsonl')}")
        return 0
    from wallexbot.gold.compare import build_shadow_report, write_shadow_report
    t0 = time.time()
    rep = build_shadow_report(args.data_dir, args.run_dir)
    jp, mp = write_shadow_report(rep, args.out_dir)
    for key, w in rep["windows"].items():
        print(f"gold-shadow {key}: shadow {len(w['shadow']['round_trips'])} round trips, "
              f"equity {w['shadow']['equity_at_bid']:.2f}; backtest {w['backtest']['trades']} trades, "
              f"equity {w['backtest']['final_equity']:.2f}")
    print(f"wrote {jp} and {mp} ({time.time() - t0:.0f}s)")
    return 0
