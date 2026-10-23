"""HINDSIGHT ORACLE - NOT A TRADABLE STRATEGY.

Uses future prices (perfect foresight) to pick the best long-only entries/exits
on the same Binance PAXGUSDT 1h data and the same cost assumptions as the real
backtest. It shows what the *data* allows, not what any algorithm can earn live.
Run: python gold/hindsight_oracle.py   -> writes gold/HINDSIGHT.md
"""
import csv, math, datetime as dt
from pathlib import Path

FEE, HALF_SPREAD = 0.002, 0.00075            # per side, same as gold/REPORT.md
SIDE = FEE + HALF_SPREAD
START = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc).timestamp()
here = Path(__file__).parent
rows = [r for r in csv.DictReader(open(here / "data/binance_PAXGUSDT_1h.csv")) if float(r["ts"]) >= START]
op = [float(r["open"]) for r in rows]; ts = [int(r["ts"]) for r in rows]
n = len(op)

def run(frac):
    """DP over bars: log-equity maximisation, trades at bar opens, long-only, fixed fraction of equity."""
    NEG = -1e18
    flat = [0.0] * (n + 1); inpos = [NEG] * (n + 1); prev = [None] * (n + 1)
    # value = best log-equity; choose in/out for each bar's open->next open interval
    f = [0.0]; p = [NEG]; trace = []
    for i in range(n - 1):
        g = op[i + 1] / op[i]
        buy = f[-1] + math.log(1 - frac * SIDE)               # enter at open i
        stay = p[-1] + math.log(1 + frac * (g - 1)) if p[-1] > NEG else NEG
        sell = p[-1] + math.log(1 - frac * SIDE) if p[-1] > NEG else NEG
        # state after bar i: in position (held through g) or flat
        in_from_buy = buy + math.log(1 + frac * (g - 1))
        in_from_stay = stay
        flat_from_flat = f[-1]
        flat_from_sell = sell
        newp, srcp = (in_from_buy, "buy") if in_from_buy >= in_from_stay else (in_from_stay, "stay")
        newf, srcf = (flat_from_flat, "flat") if flat_from_flat >= flat_from_sell else (flat_from_sell, "sell")
        f.append(newf); p.append(newp); trace.append((srcf, srcp))
    # backtrack
    state = "flat" if f[-1] >= p[-1] + math.log(1 - frac * SIDE) else "in"
    pos = [0] * (n - 1)
    for i in range(n - 2, -1, -1):
        sf, sp = trace[i]
        if state == "flat":
            src = sf; pos[i] = 0
            state = "flat" if src == "flat" else "in"
        else:
            src = sp; pos[i] = 1
            state = "flat" if src == "buy" else "in"
    # replay equity with real costs
    eq = 1.0; hist = [(ts[0], eq)]; trades = 0; held = 0
    for i in range(n - 1):
        if pos[i] and not held: eq *= 1 - frac * SIDE; trades += 1
        if not pos[i] and held: eq *= 1 - frac * SIDE
        held = pos[i]
        if held: eq *= 1 + frac * (op[i + 1] / op[i] - 1)
        hist.append((ts[i + 1], eq))
    if held: eq *= 1 - frac * SIDE; hist[-1] = (hist[-1][0], eq)
    return hist, trades

def monthly(hist):
    out = {}; last = {}
    for t, e in hist: last[dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m")] = e
    prev = 1.0
    for m in sorted(last): out[m] = last[m] / prev - 1; prev = last[m]
    return out

md = ["# HINDSIGHT ORACLE - not a strategy, not evidence\n",
      "This file uses **future prices** to choose entries/exits on the same PAXGUSDT data and the same costs "
      "(0.2% fee + 7.5 bps half-spread per side) as `REPORT.md`. It cannot be traded: it knows the outcome. "
      "It only shows that the *data* contains enough movement for 3%/month; it says **nothing** about whether "
      "wallexio or any real algorithm earns that. The honest results are in `REPORT.md` (target not met).\n"]
for frac, name in [(0.25, "25% of equity (repo cap)"), (1.0, "100% of equity")]:
    hist, trades = run(frac); mo = monthly(hist)
    md.append(f"\n## {name}\n\n total {hist[-1][1]-1:+.1%}, {trades} trades\n\n| month | return |\n|---|---|")
    md += [f"| {m} | {r:+.2%} |" for m, r in mo.items()]
(here / "HINDSIGHT.md").write_text("\n".join(md) + "\n")
print("\n".join(md))
