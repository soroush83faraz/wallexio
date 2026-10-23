"""Gold backtest: the pre-registered G1 runs (gold/README.md) on the repo's backtest engine.

Everything here is offline and deterministic: it reads the committed gold/data/ files, runs
backtest/engine.py with the repo's cost model and risk config, and writes gold/report.json plus
gold/REPORT.md rendered ONLY from that JSON (so the two cannot disagree).
"""
from __future__ import annotations

import calendar
import json
import math
import time
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from wallexbot.backtest.costs import CostModel
from wallexbot.backtest.engine import (ENGINE_VERSION, BacktestParams, BacktestResult, LossStreakCooldown,
                                       Trade, run_backtest)
from wallexbot.backtest.metrics import max_drawdown, trade_stats
from wallexbot.config import DEFAULT_PATH, RiskConfig, load_config
from wallexbot.data.candles import Candle, sidecar_path
from wallexbot.gold import data as gdata
from wallexbot.gold.strategy import STRATEGY_VERSION, G1Engine, G1Params, G1Trail
from wallexbot.types import SymbolRules, round_down

SYMBOL = "PAXGUSDT"
EVAL_START_TS = calendar.timegm((2026, 4, 1, 0, 0, 0))
INITIAL_EQUITY = Decimal(1000)
TARGET_MONTHLY = 0.03
MONTH_DAYS = 30.4375
H1 = 3600
REPORT_VERSION = "gold-report-1"


# ------------------------------------------------------------------ variants (pre-registered)
@dataclass(frozen=True)
class Variant:
    id: str
    label: str
    params: G1Params = field(default_factory=G1Params)
    taker_rate: Decimal = Decimal("0.002")
    half_spread_bps: Decimal = Decimal("7.5")
    impact_at_cap: bool = False
    uncapped: bool = False

    def cost_model(self) -> CostModel:
        return CostModel(taker_rate=self.taker_rate, maker_rate=self.taker_rate, half_spread_bps=self.half_spread_bps,
                         impact_k=Decimal(10) ** 6 if self.impact_at_cap else Decimal("0.2"),
                         impact_cap_bps=Decimal(50))


def variants(paxg_half_spread_bps: Decimal) -> list[Variant]:
    base = G1Params()
    return [
        Variant("V0", "G1 as pre-registered (headline)"),
        Variant("V1", "stops x0.8 (1.6 / 2.4 ATR)", replace(base, stop_atr_mult=Decimal("1.6"), trail_atr_mult=Decimal("2.4"))),
        Variant("V2", "stops x1.2 (2.4 / 3.6 ATR)", replace(base, stop_atr_mult=Decimal("2.4"), trail_atr_mult=Decimal("3.6"))),
        Variant("V3", "lookbacks x0.8 (EMA 40d, breakout 16d)", replace(base, ema_days=40, breakout_days=16)),
        Variant("V4", "lookbacks x1.2 (EMA 60d, breakout 24d)", replace(base, ema_days=60, breakout_days=24)),
        Variant("S1", "V0, fees x2 (0.4 % per side)", taker_rate=Decimal("0.004")),
        Variant("S2", "V0, impact always at the 50 bps cap", impact_at_cap=True),
        Variant("S3", "V0, position/exposure caps 100 %, no max order value", uncapped=True),
        Variant("S4", f"V0, half-spread {paxg_half_spread_bps} bps (observed Wallex PAXGUSDT)",
                half_spread_bps=paxg_half_spread_bps),
    ]


@dataclass(frozen=True)
class GoldCooldown:
    """Engine cooldown hook: no entry for `bars_after_exit` bars after this symbol's last exit, and
    the repo's loss-streak cooldown (RiskConfig.consecutive_loss_cooldown losses -> 3600 s)."""
    bar_s: int
    bars_after_exit: int
    n_losses: int
    loss_cooldown_s: int

    def __call__(self, symbol: str, ts: int, closed: Sequence[Trade]) -> bool:
        mine = [t for t in closed if t.symbol == symbol]
        if mine and ts - mine[-1].exit_ts < self.bars_after_exit * self.bar_s:
            return True
        return LossStreakCooldown(self.n_losses, self.loss_cooldown_s)(symbol, ts, closed)


def repo_risk() -> RiskConfig:
    """The packaged default config's risk block (never $WALLEXBOT_CONFIG: the report must reproduce)."""
    return load_config(DEFAULT_PATH, env={}).risk


def variant_risk(v: Variant) -> RiskConfig:
    risk = repo_risk()
    if v.uncapped:
        risk = replace(risk, max_position_pct=Decimal(100), max_exposure_pct=Decimal(100),
                       max_order_value=Decimal(10) ** 12)
    return risk


def run_variant(bars: Sequence[Candle], rules: SymbolRules, v: Variant, *, trade_start_ts: int = EVAL_START_TS,
                initial_equity: Decimal = INITIAL_EQUITY) -> BacktestResult:
    risk = variant_risk(v)
    cooldown = GoldCooldown(H1, v.params.cooldown_bars, risk.consecutive_loss_cooldown, 3600)
    params = BacktestParams(bar_interval_s=H1, trade_start_ts=trade_start_ts, lookback_bars=v.params.lookback_bars)
    return run_backtest({rules.symbol: list(bars)}, G1Engine(v.params), G1Trail(v.params), v.cost_model(), risk,
                        {rules.symbol: rules}, initial_equity, params, cooldown=cooldown)


# ------------------------------------------------------------------ statistics
def r8(x: float | None) -> float | None:
    """Floats in the report are rounded to 8 significant digits (stable text across runs)."""
    if x is None or not math.isfinite(x):
        return None
    return float(f"{x:.8g}")


def month_of(ts: int) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


def month_end(month: str) -> int:
    y, m = int(month[:4]), int(month[5:])
    return calendar.timegm((y + (m == 12), m % 12 + 1, 1, 0, 0, 0))


def monthly_table(points: Sequence[tuple[int, float]], start_ts: int, start_equity: float,
                  trade_exits: Sequence[int] = ()) -> list[dict]:
    """points: (close_ts, equity) ascending, all with close_ts > start_ts. One row per calendar
    month: return from the previous month-end equity (start_equity for the first) to this month's
    last point. `complete` is False for a month the data does not cover to its end."""
    rows: list[dict] = []
    prev = start_equity
    months: dict[str, list[tuple[int, float]]] = {}
    for ts, eq in points:
        months.setdefault(month_of(ts - 1), []).append((ts, eq))   # a close at 00:00 ends the previous month
    for m in sorted(months):
        last_ts, last_eq = months[m][-1]
        rows.append({"month": m, "return": r8(last_eq / prev - 1), "end_equity": r8(last_eq),
                     "complete": last_ts >= month_end(m),
                     "trades_closed": sum(1 for t in trade_exits if month_of(t) == m)})
        prev = last_eq
    return rows


def window_stats(points: Sequence[tuple[int, float]], start_ts: int, start_equity: float) -> dict:
    final_ts, final = points[-1]
    total = final / start_equity - 1
    days = (final_ts - start_ts) / 86400
    geo = math.exp(math.log1p(total) * MONTH_DAYS / days) - 1 if total > -1 and days > 0 else None
    return {"start_ts": start_ts, "end_ts": final_ts, "days": r8(days), "start_equity": r8(start_equity),
            "final_equity": r8(final), "total_return": r8(total), "geo_avg_monthly_return": r8(geo),
            "max_drawdown": r8(max_drawdown([start_equity] + [e for _, e in points]))}


def target_block(monthly: Sequence[dict], geo: float | None) -> dict:
    complete = [r["return"] for r in monthly if r["complete"]]
    return {"target_monthly": TARGET_MONTHLY, "geo_avg_monthly_return": geo,
            "arith_mean_complete_months": r8(sum(complete) / len(complete)) if complete else None,
            "complete_months": len(complete),
            "complete_months_at_or_above_target": sum(1 for x in complete if x >= TARGET_MONTHLY),
            "met": bool(geo is not None and geo >= TARGET_MONTHLY)}


def summarize(result: BacktestResult, start_ts: int, *, with_trades: bool = False) -> dict:
    init = float(result.initial_equity)
    pts = [(p.ts + H1, float(p.equity)) for p in result.equity_curve if p.ts + H1 > start_ts]
    curve = [p for p in result.equity_curve if p.ts + H1 > start_ts]
    trades = result.trades
    ts_ = trade_stats(trades)
    fees = sum((t.fees for t in trades), Decimal(0))
    spread = sum((t.spread_cost for t in trades), Decimal(0))
    impact = sum((t.impact_cost for t in trades), Decimal(0))
    gross = sum((t.gross_pnl for t in trades), Decimal(0))
    in_pos = [p for p in curve if p.position_value > 0]
    w = window_stats(pts, start_ts, init)
    monthly = monthly_table(pts, start_ts, init, [t.exit_ts for t in trades])
    events: dict[str, int] = {}
    for e in result.events:
        key = f"{e.kind}:{e.detail.split(',')[0]}" if e.detail else e.kind
        events[key] = events.get(key, 0) + 1
    out = {
        **w,
        "monthly": monthly,
        "target": target_block(monthly, w["geo_avg_monthly_return"]),
        "trades": ts_["trade_count"], "wins": ts_["wins"], "losses": ts_["losses"],
        "win_rate": r8(ts_["win_rate"]), "profit_factor": r8(ts_["profit_factor"]),
        "avg_win": r8(ts_["avg_win"]), "avg_loss": r8(ts_["avg_loss"]),
        "expectancy_usdt": r8(ts_["expectancy_quote"]), "expectancy_r": r8(ts_["expectancy_r"]),
        "gross_pnl": r8(float(gross)), "net_pnl": r8(ts_["net_pnl"]),
        "fees": r8(float(fees)), "spread_cost": r8(float(spread)), "impact_cost": r8(float(impact)),
        "total_costs": r8(float(fees + spread + impact)),
        "exposure_time": r8(len(in_pos) / len(curve)) if curve else None,
        "avg_position_pct_when_in": r8(sum(float(p.position_value / p.equity) for p in in_pos) / len(in_pos))
        if in_pos else None,
        "end_of_data_trades": sum(1 for t in trades if t.end_of_data),
        "events": dict(sorted(events.items())),
        "inputs_hash": result.inputs_hash, "result_hash": result.result_hash,
    }
    if with_trades:
        out["trade_list"] = [trade_row(t) for t in trades]
    return out


def trade_row(t: Trade) -> dict:
    exit_qty = sum((x.qty for x in t.exits), Decimal(0))
    exit_px = sum((x.qty * x.price for x in t.exits), Decimal(0)) / exit_qty
    return {"entry_ts": t.entry_ts, "entry_utc": utc(t.entry_ts), "entry_price": str(t.entry_price),
            "qty": str(t.qty), "notional": r8(float(t.qty * t.entry_price)), "initial_stop": str(t.initial_stop),
            "exit_ts": t.exit_ts, "exit_utc": utc(t.exit_ts), "exit_price": r8(float(exit_px)),
            "exit_reason": t.exit_reason, "bars_held": t.bars_held, "net_pnl": r8(float(t.net_pnl)),
            "costs": r8(float(t.fees + t.spread_cost + t.impact_cost)), "r_multiple": r8(float(t.r_multiple)),
            "end_of_data": t.end_of_data}


def utc(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def buy_and_hold(bars: Sequence[Candle], start_ts: int, fraction: Decimal, cost_model: CostModel,
                 rules: SymbolRules, initial_equity: Decimal = INITIAL_EQUITY) -> dict:
    """Buy `fraction` of equity at the first evaluation bar's OPEN, hold, sell at the last CLOSE; same
    cost model as the strategy (taker fee, half-spread, impact on the previous bar's notional)."""
    i0 = next(i for i, c in enumerate(bars) if c.ts >= start_ts)
    first, prev = bars[i0], bars[i0 - 1] if i0 > 0 else None
    liq = prev.volume * prev.close if prev is not None else None
    budget = initial_equity * fraction
    unit = cost_model.fill("BUY", first.open, Decimal(1), liq, rules.price_precision)
    qty = round_down(budget / (unit.price * (1 + cost_model.taker_rate)), rules.amount_precision)
    step = Decimal(1).scaleb(-rules.amount_precision)
    while (buy := cost_model.fill("BUY", first.open, qty, liq, rules.price_precision)).notional + buy.fee > budget:
        qty -= step
    cash = initial_equity - buy.notional - buy.fee
    eval_bars = bars[i0:]
    last, before = eval_bars[-1], bars[-2]
    sell = cost_model.fill("SELL", last.close, qty, before.volume * before.close, rules.price_precision)
    pts = [(c.ts + H1, float(cash + qty * c.close)) for c in eval_bars[:-1]]
    pts.append((last.ts + H1, float(cash + sell.notional - sell.fee)))
    w = window_stats(pts, start_ts, float(initial_equity))
    monthly = monthly_table(pts, start_ts, float(initial_equity))
    return {**w, "fraction": str(fraction), "qty": str(qty), "entry_price": str(buy.price),
            "exit_price": str(sell.price), "costs": r8(float(buy.fee + sell.fee + buy.spread_cost + sell.spread_cost
                                                                 + buy.impact_cost + sell.impact_cost)),
            "paxg_move": r8(float(last.close / first.open - 1)), "monthly": monthly,
            "target": target_block(monthly, w["geo_avg_monthly_return"])}


# ------------------------------------------------------------------ the report
def file_digest(path: Path) -> str | None:
    sc = sidecar_path(path)
    return sc.read_text().strip() if sc.is_file() else None


def build_report(data_dir: Path | str = gdata.DEFAULT_DATA_DIR) -> dict:
    d = gdata.load(data_dir)
    rules = gdata.symbol_rules(d, SYMBOL)
    depth = gdata.depth_report(d.depth) if d.depth else {}
    paxg_hs = Decimal(f"{depth['PAXGUSDT']['median_half_spread_bps']:.2f}") if "PAXGUSDT" in depth else Decimal("42")
    bars = d.h1
    runs = {}
    for v in variants(paxg_hs):
        res = run_variant(bars, rules, v)
        runs[v.id] = {"label": v.label, "params": params_dict(v), **summarize(res, EVAL_START_TS,
                                                                             with_trades=v.id in ("V0", "S3"))}
    base_cost = variants(paxg_hs)[0].cost_model()
    bh = {"100pct": buy_and_hold(bars, EVAL_START_TS, Decimal(1), base_cost, rules),
          "25pct": buy_and_hold(bars, EVAL_START_TS, Decimal("0.25"), base_cost, rules)}
    ddir = Path(data_dir)
    return {
        "report_version": REPORT_VERSION, "engine_version": ENGINE_VERSION, "strategy_version": STRATEGY_VERSION,
        "symbol_traded": SYMBOL, "symbol_rules": {"amount_precision": rules.amount_precision,
                                                  "price_precision": rules.price_precision},
        "data": {"bars": "Binance PAXGUSDT 1h", "n_bars": len(bars), "first_ts": bars[0].ts, "last_ts": bars[-1].ts,
                 "first_utc": utc(bars[0].ts), "last_close_utc": utc(bars[-1].ts + H1),
                 "sha256": {f: file_digest(ddir / f) for f in
                            (gdata.F_BINANCE_1H, gdata.F_BINANCE_5M, gdata.wallex_file("PAXGUSDT"),
                             gdata.wallex_file("XAUTUSDT"))},
                 "fetched_at_utc": d.meta.get("fetched_at_utc")},
        "windows": {"warmup_start_utc": utc(bars[0].ts), "eval_start_ts": EVAL_START_TS,
                    "eval_start_utc": utc(EVAL_START_TS), "eval_end_utc": utc(bars[-1].ts + H1)},
        "initial_equity": float(INITIAL_EQUITY),
        "risk_config": {k: str(val) for k, val in sorted(asdict(repo_risk()).items())},
        "variants": runs,
        "buy_and_hold": bh,
        "wallex": {"markets": d.markets, "depth": depth,
                   "basis_vs_binance_paxg": {s: gdata.basis_report(bars, w) for s, w in sorted(d.wallex_h1.items())}},
    }


def params_dict(v: Variant) -> dict:
    p = v.params
    return {"ema_days": p.ema_days, "breakout_days": p.breakout_days, "atr_days": p.atr_days,
            "stop_atr_mult": str(p.stop_atr_mult), "trail_atr_mult": str(p.trail_atr_mult),
            "cooldown_bars": p.cooldown_bars, "taker_rate": str(v.taker_rate),
            "half_spread_bps": str(v.half_spread_bps), "impact": "cap 50 bps" if v.impact_at_cap
            else "0.2 x notional / prev bar notional, cap 50 bps", "position_caps": "100/100, no max order"
            if v.uncapped else "repo: 25 % position, 60 % exposure, 1000 USDT max order"}


def dumps(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def write_report(report: Mapping[str, Any], out_dir: Path | str = Path("gold")) -> tuple[Path, Path]:
    from wallexbot.gold.report import render_markdown
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp, mp = out / "report.json", out / "REPORT.md"
    text = dumps(report)
    jp.write_text(text, encoding="utf-8", newline="\n")
    mp.write_text(render_markdown(json.loads(text)), encoding="utf-8", newline="\n")
    return jp, mp
