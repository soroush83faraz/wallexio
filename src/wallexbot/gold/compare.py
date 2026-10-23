"""Shadow replay vs pure backtest over the same window -> gold/shadow.json and gold/SHADOW.md.

The shadow side is the real bot path (gold/shadow.py run_replay); the backtest side is the same G1
(V0) on backtest/engine.py over the same bars. SHADOW.md is rendered only from shadow.json.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from wallexbot.gold import backtest as gb
from wallexbot.gold import data as gdata
from wallexbot.gold import shadow as gs
from wallexbot.gold.report import num, pct
from wallexbot.types import SymbolRules

H1 = 3600
SHADOW_VERSION = "gold-shadow-1"


def shadow_trips(fills: Sequence[Mapping[str, Any]], rules: SymbolRules) -> list[dict]:
    """Round trips from ShadowBroker fill events: a BUY opens; SELLs close it once what is held
    (bought minus the base-asset buy fee) is sold down to less than one amount step."""
    step = Decimal(1).scaleb(-rules.amount_precision)
    trips: list[dict] = []
    cur: dict | None = None
    for f in fills:
        qty, px, fee = Decimal(f["quantity"]), Decimal(f["price"]), Decimal(f["fee"])
        if f["side"] == "BUY":
            if cur is None:
                cur = {"entry_ts": f["ts"], "bought": Decimal(0), "buy_quote": Decimal(0), "buy_fee_base": Decimal(0),
                       "sold": Decimal(0), "sell_quote": Decimal(0), "sell_fee": Decimal(0), "exit_ts": None,
                       "last_sell_px": None}
            cur["bought"] += qty
            cur["buy_quote"] += qty * px
            cur["buy_fee_base"] += fee
        elif cur is not None:
            cur["sold"] += qty
            cur["sell_quote"] += qty * px
            cur["sell_fee"] += fee
            cur["exit_ts"], cur["last_sell_px"] = f["ts"], px
            if cur["bought"] - cur["buy_fee_base"] - cur["sold"] < step:
                trips.append(_trip_row(cur, step))
                cur = None
    if cur is not None:
        trips.append(_trip_row(cur, step))
    return trips


def _trip_row(c: Mapping[str, Any], step: Decimal) -> dict:
    entry_px = c["buy_quote"] / c["bought"]
    exit_px = c["sell_quote"] / c["sold"] if c["sold"] > 0 else None
    left = c["bought"] - c["buy_fee_base"] - c["sold"]
    dust_value = left * (c["last_sell_px"] or entry_px)
    pnl = c["sell_quote"] - c["sell_fee"] - c["buy_quote"] + dust_value
    fees = c["buy_fee_base"] * entry_px + c["sell_fee"]
    return {"entry_ts": int(c["entry_ts"]), "entry_utc": gb.utc(int(c["entry_ts"])), "entry_price": gb.r8(float(entry_px)),
            "qty": str(c["bought"]), "exit_ts": None if c["exit_ts"] is None else int(c["exit_ts"]),
            "exit_utc": None if c["exit_ts"] is None else gb.utc(int(c["exit_ts"])),
            "exit_price": None if exit_px is None else gb.r8(float(exit_px)), "fees_usdt": gb.r8(float(fees)),
            "net_pnl": gb.r8(float(pnl)), "closed": c["exit_ts"] is not None and left < step}


def backtest_trips(res) -> list[dict]:
    out = []
    for t in res.trades:
        row = gb.trade_row(t)
        out.append({"entry_ts": row["entry_ts"], "entry_utc": row["entry_utc"], "entry_price": gb.r8(float(t.entry_price)),
                    "qty": row["qty"], "exit_ts": row["exit_ts"], "exit_utc": row["exit_utc"],
                    "exit_price": row["exit_price"], "fees_usdt": gb.r8(float(t.fees)), "net_pnl": row["net_pnl"],
                    "exit_reason": row["exit_reason"], "end_of_data": t.end_of_data})
    return out


def pair(shadow: Sequence[dict], backtest: Sequence[dict]) -> list[dict]:
    """Match each backtest round trip with the first unused shadow round trip whose holding period
    overlaps it; unmatched ones are listed alone."""
    rows, used = [], set()
    for b in backtest:
        b_end = b["exit_ts"] if b["exit_ts"] is not None else float("inf")
        m = next((i for i, s in enumerate(shadow) if i not in used and s["entry_ts"] <= b_end
                  and (s["exit_ts"] if s["exit_ts"] is not None else float("inf")) >= b["entry_ts"]), None)
        s = shadow[m] if m is not None else None
        if m is not None:
            used.add(m)
        rows.append({"backtest": b, "shadow": s, **(_diff(b, s) if s is not None else {})})
    rows += [{"backtest": None, "shadow": s} for i, s in enumerate(shadow) if i not in used]
    return rows


def risk_rejections(db: Path) -> list[dict]:
    """Entries the repo's risk engine refused in the shadow run (from the audit store)."""
    import sqlite3
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT ts, client_id, reasons FROM risk_decisions WHERE approved = 0 ORDER BY id").fetchall()
    finally:
        con.close()
    return [{"ts": int(ts), "utc": gb.utc(int(ts)), "client_id": cid, "reasons": json.loads(reasons)}
            for ts, cid, reasons in rows]


def _diff(b: Mapping[str, Any], s: Mapping[str, Any]) -> dict:
    def bps(x, y):
        return None if x is None or y is None else gb.r8((x / y - 1) * 10000)
    return {"entry_delay_s": s["entry_ts"] - b["entry_ts"],
            "exit_delay_s": None if s["exit_ts"] is None or b["exit_ts"] is None else s["exit_ts"] - b["exit_ts"],
            "entry_price_diff_bps": bps(s["entry_price"], b["entry_price"]),
            "exit_price_diff_bps": bps(s["exit_price"], b["exit_price"]),
            "pnl_diff_usdt": gb.r8(s["net_pnl"] - b["net_pnl"])}


def compare_window(data: gdata.GoldData, run_dir: Path, name: str, start_ts: int, end_ts: int,
                   quotes, quote_s: int) -> dict:
    db, journal = run_dir / f"gold_shadow_{name}.db", run_dir / f"gold_shadow_{name}_journal.jsonl"
    rules = gdata.symbol_rules(data, gb.SYMBOL)
    shadow = gs.run_replay(data, db, journal, start_ts=start_ts, end_ts=end_ts, quotes=quotes, quote_s=quote_s)
    fills = gs.journal_fills(journal)
    bars = [c for c in data.h1 if c.ts + H1 <= end_ts]
    v0 = gb.variants(Decimal("42"))[0]
    res = gb.run_variant(bars, rules, v0, trade_start_ts=start_ts - H1)
    bt_final = float(res.final_equity)
    s_trips, b_trips = shadow_trips(fills, rules), backtest_trips(res)
    return {
        "window": {"start_utc": gb.utc(start_ts), "end_utc": gb.utc(end_ts), "start_ts": start_ts, "end_ts": end_ts,
                   "quote_candles": f"Binance PAXGUSDT {quote_s // 60}m", "decision_bars": "Binance PAXGUSDT 1h"},
        "files": {"db": db.as_posix(), "journal": journal.as_posix()},
        "shadow": {**shadow, "net_return_at_bid": gb.r8(shadow["equity_at_bid"] / 1000 - 1),
                   "fills": [{k: f[k] for k in ("ts", "side", "type", "quantity", "price", "fee", "fee_asset",
                                                "liquidity", "slippage_bps")} for f in fills],
                   "round_trips": s_trips, "risk_rejections": risk_rejections(db)},
        "backtest": {"final_equity": gb.r8(bt_final), "net_return": gb.r8(bt_final / 1000 - 1),
                     "trades": len(res.trades), "round_trips": b_trips,
                     "fills": [{"ts": f.ts, "side": f.side, "kind": f.kind, "qty": str(f.qty), "price": str(f.price),
                                "fee": gb.r8(float(f.fee))} for f in res.fills],
                     "result_hash": res.result_hash},
        "paired": pair(s_trips, b_trips),
    }


def build_shadow_report(data_dir: Path | str = gdata.DEFAULT_DATA_DIR, run_dir: Path | str = gs.DEFAULT_RUN_DIR) -> dict:
    data = gdata.load(data_dir)
    run_dir = Path(run_dir)
    s30, e30 = gs.shadow_window(data)
    full_end = min(data.h1[-1].ts + H1, data.m15[-1].ts + gdata.M15) // H1 * H1
    return {
        "shadow_version": SHADOW_VERSION,
        "approach": "real bot path: bot.loop.Bot -> pipeline.route_to_oms -> OMS (risk engine, kill switch, SQLite "
                    "audit store) -> ShadowBroker (simulated fills, JSONL journal), in-process guardian; offline replay "
                    "feed of committed Binance data with a synthetic +/-7.5 bps book; nothing is sent anywhere",
        "windows": {
            "last_30_days": compare_window(data, run_dir, "30d", s30, e30, data.m5, 300),
            "full_eval_window": compare_window(data, run_dir, "full", gb.EVAL_START_TS, full_end, data.m15, 900),
        },
    }


def write_shadow_report(rep: Mapping[str, Any], out_dir: Path | str = Path("gold")) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp, mp = out / "shadow.json", out / "SHADOW.md"
    text = gb.dumps(rep)
    jp.write_text(text, encoding="utf-8", newline="\n")
    mp.write_text(render_shadow(json.loads(text)), encoding="utf-8", newline="\n")
    return jp, mp


def _delay(s: int | None) -> str:
    if s is None:
        return "n/a"
    return f"{s / 3600:+.2f} h" if abs(s) >= 3600 else f"{s:+d} s"


RISK_REJECTION_NOTE = ("- **Risk-engine rejections (a finding).** The bot sizes with `size_position` (25 % of equity at the "
          "limit price) and the OMS then runs the risk engine, which adds the value of any PAXG still held to the "
          "new order. ShadowBroker charges the buy fee in PAXG, so after the first round trip a dust remainder "
          "(below one amount step, unsellable) stays in the account; a new entry sized to exactly the 25 % cap "
          "then exceeds it by that dust and is refused (`above_max_position_pct`). The backtest engine sizes and "
          "checks with one formula and never sees this. The refused entries are listed under each window "
          "above; the entry happens only on a later signal whose rounding leaves room under the cap. This is a sizing-vs-risk mismatch in the "
          "repo's bot path that only shows when the position cap binds; it is reported, not patched here.")


def render_shadow(rep: Mapping[str, Any]) -> str:
    L = ["# Gold shadow replay vs backtest (wallexio)", "",
         "Generated by `python -m wallexbot gold-shadow` from the committed files in `gold/data/`; every number "
         "below is read from `gold/shadow.json`. No network, no orders: the broker is the repo's ShadowBroker.", "",
         "**Which shadow this is:** the strategy runs inside the repo's real bot path, not a re-implementation: "
         + rep["approach"] + ". The audit SQLite database and the broker journal of each run are written under "
         "`runs/` (git-ignored).", ""]
    for key, title in (("last_30_days", "Last 30 days (pre-registered window, 5m quotes)"),
                       ("full_eval_window", "Whole evaluation window (added to exercise fills, 15m quotes)")):
        w = rep["windows"][key]
        sh, bt = w["shadow"], w["backtest"]
        L += [f"## {title}", "",
              f"{w['window']['start_utc']} to {w['window']['end_utc']} UTC; decisions on {w['window']['decision_bars']}, "
              f"quotes from {w['window']['quote_candles']} closes. Files: `{w['files']['db']}`, `{w['files']['journal']}`.", ""]
        L += _tbl(["", "shadow (bot path)", "backtest (engine)"], [
            ["round trips", str(len(sh["round_trips"])), str(len(bt["round_trips"]))],
            ["fills", str(len(sh["fills"])), str(len(bt["fills"]))],
            ["final equity USDT", f"{num(sh['equity_at_bid'])} (at bid)", num(bt["final_equity"])],
            ["net return", pct(sh["net_return_at_bid"]), pct(bt["net_return"])],
            ["bot ticks / ticks with errors", f"{sh['ticks']} / {sh['ticks_with_errors']}", ""],
            ["intents by state", ", ".join(f"{k} {n}" for k, n in sh["intents_by_state"].items()) or "none", ""],
            ["kill switch engaged at end", "yes" if sh["kill_switch_engaged"] else "no", ""],
            ["guardian ticks / failed", f"{sh['guardian']['ticks']} / {sh['guardian']['failed']}" if sh["guardian"] else "off", ""],
            ["errors table rows", str(sh["errors_rows"]), ""],
        ])
        L += ["", "Decision log (audit_events by kind): "
              + ", ".join(f"{k} {n}" for k, n in sh["decisions"].items()) + ".", ""]
        if w["paired"]:
            L += _tbl(["entry bt / shadow (UTC)", "entry px bt / shadow", "exit bt / shadow (UTC)", "exit px bt / shadow",
                       "P&L bt / shadow USDT", "entry px diff", "exit px diff", "entry / exit delay"],
                      [[f"{_g(p['backtest'], 'entry_utc')} / {_g(p['shadow'], 'entry_utc')}",
                        f"{num(_g(p['backtest'], 'entry_price', None))} / {num(_g(p['shadow'], 'entry_price', None))}",
                        f"{_g(p['backtest'], 'exit_utc')} / {_g(p['shadow'], 'exit_utc')}",
                        f"{num(_g(p['backtest'], 'exit_price', None))} / {num(_g(p['shadow'], 'exit_price', None))}",
                        f"{num(_g(p['backtest'], 'net_pnl', None))} / {num(_g(p['shadow'], 'net_pnl', None))}",
                        _bps(p.get("entry_price_diff_bps")), _bps(p.get("exit_price_diff_bps")),
                        f"{_delay(p.get('entry_delay_s'))} / {_delay(p.get('exit_delay_s'))}"] for p in w["paired"]])
            L += [""]
        if sh["risk_rejections"]:
            L += ["Entries refused by the repo's risk engine in the shadow run (the backtest engine has no such "
                  "second check): " + "; ".join(f"{r['utc']} UTC {', '.join(r['reasons'])}" for r in sh["risk_rejections"])
                  + ".", ""]
        else:
            L += ["No signal in this window, on either side: nothing to compare beyond the fact that both stayed flat.", ""]
    L += ["## Why shadow and backtest differ", "",
          "- **Entry.** The engine fills at the next 1h bar's open plus half-spread and impact. The bot evaluates the "
          "closed bar at the first tick after it closes, sends a LIMIT at ask + 10 bps through the OMS, and "
          "ShadowBroker fills it about 2 s later at the synthetic ask (the last quote close + 7.5 bps).",
          "- **Stops.** The engine assumes a touched stop fills exactly at the stop (or at the open after a gap) "
          "using the 1h low. The bot only sees quote closes (5m / 15m): it sells at the bid after a close below the "
          "stop (usually below the stop level), or, if only a wick inside one quote candle touched it, on the next "
          "1h close via its `stop_touched_on_bar` check. That is the main source of exit-price differences.",
          "- **Fees.** ShadowBroker charges the buy fee in the base asset (PAXG) and the sell fee in USDT; the engine "
          "charges both in USDT. Same value, slightly different quantities (the bot sells what it actually holds).",
          "- **Sizing and rounding.** Both size with the repo's `size_position` (3 % risk, 25 % position cap), the "
          "engine at open x (1 + half-spread), the bot at its limit price and current equity; stops are put on the "
          "price tick by the engine only.",
          *([RISK_REJECTION_NOTE] if any(w["shadow"]["risk_rejections"] for w in rep["windows"].values()) else []),
          "- **Cooldown.** Both wait 24 bars after an exit; the bot's tracker also refuses a re-entry on the same "
          "breakout level after a losing stop-out.",
          "- **Book.** The replay book is synthetic and deep (no impact); the real Wallex PAXGUSDT book is far "
          "thinner and wider (REPORT.md, 'Wallex reality check'). `gold-shadow --live` runs the same stack on the "
          "real Wallex book.", ""]
    return "\n".join(L)


def _g(row: Mapping[str, Any] | None, key: str, default: Any = "-") -> Any:
    if row is None:
        return default
    v = row.get(key)
    return default if v is None else v


def _bps(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f} bps"


def _tbl(head: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return (["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
            + ["| " + " | ".join(r) + " |" for r in rows])
