"""Backtest runner: history -> closed 15m bars -> chronological split -> S0/S1 x E1..E4 x fee grid on
TRAIN and VALIDATION only -> JSON + Markdown reports. Exploratory tooling; nothing here validates a strategy.

Split (documented, fixed before any run; code constants, not tuned):
    [ train 50% ][ embargo 300 bars ][ validation 25% ][ embargo 300 bars ][ test (remainder, ~25%) ]
on the sorted union of 15m bar open times. The embargo equals the strategies' indicator window
(`history_bars` = 300), so no bar of one segment feeds an indicator value of the next. A segment is
backtested with up to 300 warm-up bars taken from the embargo before it (never from the previous segment)
and entries start at the segment's first bar. Warm-up equity points (flat at the initial equity) are
trimmed from the equity curve before metrics so Sharpe/annualisation are not diluted.

The TEST segment is never touched by `run_grid`: it only builds bars for train/validation, never calls
`TestSetGuard`, and the feasibility scan is limited to train+validation bars. The test segment is reserved
for `run_final_test`, which needs a frozen params file and consumes the single-use guard exactly once.

Rules for symbols missing from the markets fixture (`symbol_rules`): price_precision = the finest tick seen
in the data (max decimals of the 1m closes, capped at 8); amount_precision = the smallest p whose lot
10**-p is worth at most 0.15 USDT at the median price (the XRP fixture lot, 0.1 XRP, is ~0.14 USDT), so a
lot is never finer than known listings; min_notional stays None (the larger of that and risk.min_notional
= 5 applies). Both are conservative guesses, NOT exchange facts.

Signal evaluations are memoised per (symbol, bar, visible length[, market]) so the four exit policies and
re-runs share them; the engines are stateless and only read the trailing bars, so results are identical.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from wallexbot.backtest.costs import CostModel
from wallexbot.backtest.engine import (BacktestParams, BacktestResult, Trade, loss_streak_cooldown,
                                       run_backtest, to_canonical)
from wallexbot.backtest.metrics import buy_and_hold, compute_metrics, equal_weight_buy_and_hold
from wallexbot.backtest.report import build_report, write_report
from wallexbot.backtest.sensitivity import run_sensitivity
from wallexbot.backtest.splits import Split, TestSetGuard, chronological_split, make_test_set_id
from wallexbot.config import RiskConfig, load_config
from wallexbot.data.aggregate import aggregate_to
from wallexbot.data.candles import Candle
from wallexbot.data.history import load_history
from wallexbot.strategy.config import MomentumParams
from wallexbot.strategy.contract import MarketContext, Signal
from wallexbot.strategy.exits import ExitParams, TakeProfit, make_policy
from wallexbot.strategy.momentum import MomentumSignalEngine
from wallexbot.strategy.trend_baseline import TrendBaselineEngine
from wallexbot.types import SymbolRules, round_down

BAR_S = 900
EMBARGO_BARS = 300          # == strategies' history_bars (indicator window)
WARMUP_BARS = 300
LOOKBACK_BARS = 400         # bars handed to strategy/policy; they read <= 300, so this changes nothing
TRAIN_FRAC, VAL_FRAC = Decimal("0.5"), Decimal("0.25")
INITIAL_EQUITY = Decimal(10000)
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "XAUTUSDT", "USOONUSDT", "PEPEUSDT", "SOLUSDT")
STRATEGIES = ("S0", "S1")
EXITS = ("E1", "E2", "E3", "E4")
PRE_REGISTERED_EXIT = "E4"
# (label, taker rate, note). The primary one is the repo placeholder; the others are scenarios only.
FEE_SCENARIOS = (
    ("primary_0.0035", Decimal("0.0035"), "PLACEHOLDER taker fee (config fees.taker_rate), unverified"),
    ("assumed_0.002", Decimal("0.002"), "ASSUMED fee scenario - real fee unverified"),
    ("assumed_0.001", Decimal("0.001"), "ASSUMED fee scenario - real fee unverified"),
)
SENSITIVITY_PARAMS = ("donchian_period", "ema_period", "roc_z_min", "volume_mult", "atr_pct_min", "atr_pct_max",
                      "late_atr_mult", "expected_move_atr", "cost_mult", "min_score", "stop_atr_mult")
FIXTURE_MARKETS = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "markets.json"
LEDGER_DEFAULT = Path(__file__).resolve().parents[3] / "reports" / "test_set_ledger.jsonl"
SPLIT_NOTE = (f"50% train / 25% validation / remainder (~25%) test, by 15m bar time; embargo {EMBARGO_BARS} bars "
              f"between segments; {WARMUP_BARS}-bar warm-up from the embargo.")
EXPLORATORY = ("EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed "
               "costs, ~45 days of one market regime. Not evidence of profitability.")


# ------------------------------------------------------------------------------------------ data
def _month_span(sym_dir: Path, symbol: str) -> tuple[int, int]:
    keys = sorted(m.group(1) for p in sym_dir.glob(f"{symbol}_1m_*.csv")
                  if (m := re.fullmatch(rf"{symbol}_1m_(\d{{4}}-\d{{2}})\.csv", p.name)))
    if not keys:
        raise FileNotFoundError(f"no 1m history files for {symbol} in {sym_dir}")

    def start(key: str) -> int:
        y, mo = map(int, key.split("-"))
        return int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp())

    y, mo = map(int, keys[-1].split("-"))
    nxt = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=timezone.utc)
    return start(keys[0]), int(nxt.timestamp()) - 1


def load_bars_15m(history_dir: Path | str, symbols: Sequence[str]) -> tuple[dict[str, list[Candle]], dict]:
    """Closed 15m bars per symbol (aggregated from 1m; bars with absent minutes carry `missing` > 0) and a
    per-symbol data-quality summary. Raises if a symbol has no data."""
    hd = Path(history_dir)
    bars, quality = {}, {}
    for s in symbols:
        a, b = _month_span(hd / s, s)
        one = load_history(hd, s, a, b)
        agg = aggregate_to(one, "15m")
        if not agg:
            raise ValueError(f"{s}: no closed 15m bars")
        bars[s] = agg
        flagged = [c for c in agg if c.missing > 0]
        quality[s] = {"rows_1m": len(one), "bars_15m": len(agg), "first_ts": agg[0].ts, "last_ts": agg[-1].ts,
                      "bars_with_missing_minutes": len(flagged),
                      "missing_minutes_in_bars": sum(c.missing for c in flagged),
                      "zero_volume_bars": sum(1 for c in agg if c.volume == 0)}
    return bars, quality


def symbol_rules(symbols: Sequence[str], bars: Mapping[str, Sequence[Candle]],
                 markets_path: Path | str = FIXTURE_MARKETS) -> tuple[dict[str, SymbolRules], dict[str, str]]:
    """SymbolRules per symbol: from the markets fixture when listed there, else derived (module docstring).
    Returns (rules, {symbol: 'fixture' | 'derived'})."""
    listed = {m["symbol"]: m for m in json.loads(Path(markets_path).read_text())["result"]["markets"]}
    rules, source = {}, {}
    for s in symbols:
        m = listed.get(s)
        if m is not None and m.get("amount_precision") is not None and m.get("price_precision") is not None:
            rules[s] = SymbolRules(s, m["base_asset"], m["quote_asset"], int(m["amount_precision"]),
                                   int(m["price_precision"]))
            source[s] = "fixture"
            continue
        cs = bars[s]
        decimals = max(-c.close.as_tuple().exponent for c in cs[:: max(1, len(cs) // 500)])
        price_p = min(8, max(0, decimals))
        median = Decimal(str(statistics.median(float(c.close) for c in cs)))
        amount_p = 0
        while Decimal(1).scaleb(-amount_p) * median > Decimal("0.15") and amount_p < 8:
            amount_p += 1
        quote = "USDT"
        rules[s] = SymbolRules(s, s[: -len(quote)] if s.endswith(quote) else s, quote, amount_p, price_p)
        source[s] = "derived"
    return rules, source


def build_split(bars: Mapping[str, Sequence[Candle]]) -> Split:
    ts = sorted({c.ts for cs in bars.values() for c in cs})
    return chronological_split(ts, embargo_bars=EMBARGO_BARS, max_lookback_bars=EMBARGO_BARS,
                               train_frac=TRAIN_FRAC, val_frac=VAL_FRAC)


def git_head() -> str | None:
    """`git rev-parse HEAD` of the checkout this code runs from (None outside a repository)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_summary(split: Split) -> dict:
    out = {"note": SPLIT_NOTE, "embargo_bars": split.embargo_bars}
    for name in ("train", "validation", "test"):
        seg = split.segment(name)
        out[name] = {"bars": len(seg), "start": _iso(seg.start_ts), "end_last_bar": _iso(seg.end_ts),
                     "start_ts": seg.start_ts, "end_ts": seg.end_ts}
    return out


# ------------------------------------------------------------------------------------------ components
@dataclass(frozen=True)
class RunnerCooldown:
    """Engine cooldown hook: (a) after `n_losses` consecutive losing trades block all entries for
    `loss_cooldown_s` (RiskConfig.consecutive_loss_cooldown, plan: 60 min); (b) per symbol, after an exit block
    re-entry for `cooldown_bars` bars (`profitable_bars` after a profitable exit), the S1 cooldown of
    STRATEGY.md. The 'same breakout level after a stop-out' and 'trend intact' refinements are not modelled."""
    n_losses: int = 3
    loss_cooldown_s: int = 3600
    cooldown_bars: int = 8
    profitable_bars: int = 2
    bar_s: int = BAR_S

    def __call__(self, symbol: str, ts: int, closed: Sequence[Trade]) -> bool:
        if loss_streak_cooldown(self.n_losses, self.loss_cooldown_s)(symbol, ts, closed):
            return True
        for t in reversed(closed):
            if t.symbol == symbol:
                wait = self.profitable_bars if t.net_pnl > 0 else self.cooldown_bars
                return ts - t.exit_ts < wait * self.bar_s
        return False


@dataclass(frozen=True)
class EngineId:
    """What the engine hash records about a wrapped signal engine."""
    engine: str
    params: str


def _engine_id(inner) -> EngineId:
    """Type plus parameters of a wrapped component (`.params` if it has one, else its attributes)."""
    if isinstance(inner, TrendBaselineEngine):
        params = (f"fast={inner.fast} slow={inner.slow} donchian={inner.donchian} atr={inner.atr_period} "
                  f"stop_atr_mult={inner.stop_atr_mult} history_bars={inner.history_bars}")
    else:
        try:
            params = repr(inner.params)
        except AttributeError:
            params = ""
    return EngineId(f"{type(inner).__module__}.{type(inner).__qualname__}", params)


class CachedEngine:
    """Memoising, counting wrapper of a stateless SignalEngine (see module docstring). One wrapper per run
    (own counters), the memo dict is shared by runs on the same bars."""

    def __init__(self, inner, memo: dict, *, use_market: bool, count_from_ts: int | None = None):
        self.inner, self.memo, self.use_market, self.count_from_ts = inner, memo, use_market, count_from_ts
        self.evaluations = 0
        self.signals = 0
        self.params = _engine_id(inner)

    def evaluate(self, symbol: str, bars: Sequence[Candle], market: MarketContext) -> Signal | None:
        last = bars[-1]
        key = (symbol, last.ts, len(bars), market if self.use_market else None, self.params)
        if key in self.memo:
            sig = self.memo[key]
        else:
            sig = self.memo[key] = self.inner.evaluate(symbol, bars, market)
        if self.count_from_ts is None or last.ts >= self.count_from_ts:
            self.evaluations += 1
            self.signals += sig is not None
        return sig


def make_signal_engine(strategy: str, momentum: MomentumParams | None = None):
    if strategy == "S0":
        return TrendBaselineEngine()
    if strategy == "S1":
        return MomentumSignalEngine(momentum or MomentumParams())
    raise ValueError(f"unknown strategy {strategy!r}")


class TickExit:
    """Exit-policy wrapper that rounds DOWN the price levels a policy emits (trailing/breakeven stops and
    take-profit prices) to the symbol's price tick. Why: the engine fills a stop/target at that level and
    asserts an exact cost-attribution identity; levels computed with Decimal division (ATR, breakeven)
    carry ~60 digits, qty * level then exceeds the engine's 60-digit context and the identity check aborts
    the run. Rounding down moves a long's level by < 1 tick against us (conservative). Decisions are
    otherwise passed through unchanged."""

    def __init__(self, inner, rules: Mapping[str, SymbolRules]):
        self.inner, self.rules = inner, rules
        self.params = _engine_id(inner)

    def _tick(self, symbol: str, price: Decimal) -> Decimal:
        return round_down(price, self.rules[symbol].price_precision)

    def targets(self, state):
        return tuple(TakeProfit(self._tick(state.symbol, t.price), t.fraction, t.reason)
                     for t in self.inner.targets(state))

    def on_bar(self, state, bars):
        d = self.inner.on_bar(state, bars)
        if d.action == "update_stop" and d.new_stop is not None:
            return replace(d, new_stop=self._tick(state.symbol, d.new_stop))
        return d


def make_exit(name: str, fee: Decimal, rules: Mapping[str, SymbolRules], exit_params: ExitParams | None = None):
    """Exit policy with its breakeven fee rate set to the scenario's taker rate (E4 only uses it)."""
    inner = make_policy(name, exit_params if exit_params is not None else replace(ExitParams(), fee_rate=fee))
    return TickExit(inner, rules)


def text_fields(obj) -> dict[str, str]:
    """Dataclass -> {field: repr-text}: the hashed report parameters may not contain floats."""
    return {k: repr(v) for k, v in asdict(obj).items()}


def trim_warmup(result: BacktestResult, start_ts: int) -> BacktestResult:
    """A view of `result` without the flat warm-up equity points (before `start_ts`). The hash is recomputed
    for the view; the engine's original result hash is kept by the caller."""
    curve = tuple(p for p in result.equity_curve if p.ts >= start_ts)
    view = replace(result, equity_curve=curve)
    return replace(view, result_hash=view.compute_result_hash())


METRIC_KEYS = ("net_return", "max_drawdown", "trade_count", "win_rate", "profit_factor", "expectancy_r", "sharpe",
               "expectancy_quote", "net_pnl", "gross_pnl", "fees", "spread_cost", "impact_cost",
               "fee_share_of_gross", "spread_share_of_gross", "slippage_share_of_gross",
               "cost_bps_of_traded_notional", "traded_notional", "exposure_time", "end_of_data_trades", "bars")


def _event_counts(result: BacktestResult) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in result.events:
        if e.kind in ("entry_dropped", "entry_blocked", "entry_rejected", "signal_rejected", "signal_blocked",
                      "flatten_scheduled"):
            k = f"{e.kind}:{e.detail}"
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


@dataclass(frozen=True)
class RunSpec:
    strategy: str
    exit: str
    fee_label: str
    taker_rate: Decimal
    split: str


def run_one(bars_seg: Mapping[str, Sequence[Candle]], start_ts: int, rules: Mapping[str, SymbolRules],
            risk: RiskConfig, strategy: str, exit_name: str, fee: Decimal, memo: dict, *,
            momentum: MomentumParams | None = None, exit_params: ExitParams | None = None
            ) -> tuple[BacktestResult, BacktestResult, CachedEngine]:
    """(raw engine result, warm-up-trimmed view, the counting engine wrapper)."""
    engine = CachedEngine(make_signal_engine(strategy, momentum), memo, use_market=(strategy == "S1"),
                          count_from_ts=start_ts)
    cost = CostModel(taker_rate=fee, maker_rate=fee)
    params = BacktestParams(bar_interval_s=BAR_S, trade_start_ts=start_ts, lookback_bars=LOOKBACK_BARS)
    raw = run_backtest(bars_seg, engine, make_exit(exit_name, fee, rules, exit_params), cost, risk, rules,
                       INITIAL_EQUITY, params, cooldown=RunnerCooldown())
    return raw, trim_warmup(raw, start_ts), engine


def summarize(raw: BacktestResult, view: BacktestResult, engine: CachedEngine) -> dict:
    m = compute_metrics(view)
    row = {k: m[k] for k in METRIC_KEYS}
    row.update({"evaluations": engine.evaluations, "signals": engine.signals,
                "abstentions": engine.evaluations - engine.signals,
                "events": _event_counts(raw), "inputs_hash": raw.inputs_hash, "result_hash": raw.result_hash})
    return row


# ------------------------------------------------------------------------------------------ feasibility
def cost_gate_passes(f, p: MomentumParams, market: MarketContext) -> bool:
    """The S1 cost-gate rule of `MomentumSignalEngine.diagnose`, replicated for arbitrary market contexts
    (tests assert it equals diagnose's `cost_gate`)."""
    favourable = f.efficiency_ratio >= p.er_min and f.ema_slope > 0
    mult = p.cost_mult if favourable else p.cost_mult_tight
    return p.expected_move_atr * f.atr_pct * 1e4 >= mult * float(market.round_trip_cost_bps)


def required_atr_pct(p: MomentumParams, market: MarketContext, *, favourable: bool) -> float:
    """ATR/close at which the cost gate just passes."""
    mult = p.cost_mult if favourable else p.cost_mult_tight
    return mult * float(market.round_trip_cost_bps) / (p.expected_move_atr * 1e4)


def feasibility_for_symbol(symbol: str, bars: Sequence[Candle], ranges: Sequence[tuple[int, int]],
                           fees: Sequence[tuple[str, Decimal, str]], params: MomentumParams | None = None) -> dict:
    """For each bar of `symbol` inside one of the (start_ts, end_ts) `ranges`: would the S1 cost gate pass at
    each fee scenario? Features come from `compute_features` on the exact prefix (as the engine sees it)."""
    p = params or MomentumParams()
    engine = MomentumSignalEngine(p)
    markets = {label: CostModel(taker_rate=fee, maker_rate=fee).market_context() for label, fee, _ in fees}
    primary = fees[0][0]
    n = 0
    in_ranges = 0
    atr_pcts: list[float] = []
    gate = {label: 0 for label in markets}
    gate_and_rest = {label: 0 for label in markets}
    band_ok = 0
    rest_ok_primary = 0
    for i, c in enumerate(bars):
        if not any(a <= c.ts <= b for a, b in ranges):
            continue
        in_ranges += 1
        d = engine.diagnose(symbol, bars[max(0, i + 1 - p.history_bars): i + 1], markets[primary])
        if d is None:
            continue
        n += 1
        f = d.features
        atr_pcts.append(f.atr_pct)
        band_ok += p.atr_pct_min <= f.atr_pct <= p.atr_pct_max
        others = all(r.passed for r in d.rules if r.name not in ("cost_gate", "min_score"))
        rest_ok_primary += others
        for label, mk in markets.items():
            ok = cost_gate_passes(f, p, mk)
            gate[label] += ok
            gate_and_rest[label] += ok and others
    return {"bars_in_segments": in_ranges, "bars": n, "gate_pass": gate, "gate_and_all_other_rules_pass": gate_and_rest,
            "atr_band_ok": band_ok, "all_other_rules_pass_ignoring_cost_gate": rest_ok_primary,
            "atr_pct_median": statistics.median(atr_pcts) if atr_pcts else None,
            "atr_pct_p90": sorted(atr_pcts)[int(0.9 * (len(atr_pcts) - 1))] if atr_pcts else None,
            "atr_pct_max": max(atr_pcts) if atr_pcts else None}


# ------------------------------------------------------------------------------------------ grid
_CTX: dict[str, Any] = {}   # set before the worker pool forks; read-only afterwards


def _segment(split_name: str):
    c = _CTX
    return c["segments"][split_name]


def _grid_task(task: tuple[str, str, str, str]) -> list[tuple[RunSpec, dict]]:
    split_name, strategy, fee_label, fee_text = task
    fee = Decimal(fee_text)
    bars_seg, start_ts = _segment(split_name)
    memo: dict = {}
    rows = []
    for ex in EXITS:
        raw, view, eng = run_one(bars_seg, start_ts, _CTX["rules"], _CTX["risk"], strategy, ex, fee, memo)
        rows.append((RunSpec(strategy, ex, fee_label, fee, split_name), summarize(raw, view, eng)))
    return rows


def _feas_task(symbol: str) -> tuple[str, dict]:
    return symbol, feasibility_for_symbol(symbol, _CTX["bars"][symbol], _CTX["feas_ranges"], FEE_SCENARIOS)


def _sens_task(args: tuple[str, str, str]) -> dict:
    split_name, exit_name, fee_text = args
    fee = Decimal(fee_text)
    bars_seg, start_ts = _segment(split_name)

    def run_fn(mp: MomentumParams):
        raw, view, eng = run_one(bars_seg, start_ts, _CTX["rules"], _CTX["risk"], "S1", exit_name, fee, {},
                                 momentum=mp)
        return compute_metrics(view)["expectancy_r"]

    res = run_sensitivity(run_fn, MomentumParams(), split=split_name, pct=Decimal("0.2"),
                          only=_CTX["sens_only"])
    return {"split": res.split, "pct": str(res.pct), "base_expectancy_r": res.base_expectancy,
            "fraction_same_sign": res.fraction_same_sign,
            "neighbours": [{"param": n.param, "direction": n.direction, "value": str(n.value),
                            "expectancy_r": n.expectancy, "same_sign": n.same_sign} for n in res.neighbours]}


def _pool_map(fn, items: Sequence, workers: int) -> list:
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ProcessPoolExecutor(max_workers=min(workers, len(items)),
                             mp_context=multiprocessing.get_context("fork")) as ex:
        return list(ex.map(fn, items))


def choose_top_candidate(rows: Sequence[tuple[RunSpec, dict]]) -> tuple[str, str]:
    """The S1 variant (exit policy, primary fee) with the best VALIDATION net return among those with trades;
    ties and 'no S1 trades anywhere' resolve to the pre-registered E4. Never looks at train or test."""
    cands = [(r["net_return"], s.exit) for s, r in rows
             if s.strategy == "S1" and s.split == "validation" and s.fee_label == FEE_SCENARIOS[0][0]
             and r["trade_count"] > 0]
    if not cands:
        return "S1", PRE_REGISTERED_EXIT
    best = max(c[0] for c in cands)
    tied = sorted(e for v, e in cands if v == best)
    return "S1", PRE_REGISTERED_EXIT if PRE_REGISTERED_EXIT in tied else tied[0]


def benchmark_rows(split: Split, bars: Mapping[str, Sequence[Candle]], rules: Mapping[str, SymbolRules]) -> list[dict]:
    out = []
    for name in ("train", "validation"):
        seg = split.segment(name)
        seg_bars = {s: [c for c in cs if seg.start_ts <= c.ts <= seg.end_ts] for s, cs in sorted(bars.items())}
        for label, fee, _ in FEE_SCENARIOS:
            cm = CostModel(taker_rate=fee, maker_rate=fee)
            res = {}
            per = {}
            for s, cs in seg_bars.items():
                per[s] = buy_and_hold(cs, INITIAL_EQUITY, cm, rules[s].amount_precision)
            rets = [v["net_return"] for v in per.values() if v["net_return"] is not None]
            res = {"net_return": sum(rets) / len(rets) if rets else None,
                   "max_drawdown_worst_symbol": max((v["max_drawdown"] for v in per.values()
                                                     if v["max_drawdown"] is not None), default=None),
                   "per_symbol_net_return": {s: v["net_return"] for s, v in per.items()}}
            out.append({"split": name, "fee_label": label, "taker_rate": str(fee), **res})
    return out


def run_grid(history_dir: Path | str, out_dir: Path | str, *, symbols: Sequence[str] = DEFAULT_SYMBOLS,
             workers: int = 4, markets_path: Path | str = FIXTURE_MARKETS,
             sens_only: Sequence[str] = SENSITIVITY_PARAMS) -> dict:
    """Run the whole exploratory grid on train and validation and write the reports. Returns the report dict.
    Never reads the TestSetGuard and never builds a backtest on the test segment."""
    bars, quality = load_bars_15m(history_dir, symbols)
    rules, rule_src = symbol_rules(symbols, bars, markets_path)
    split = build_split(bars)
    risk = load_config(env={}).risk
    segments = {name: split.bars_for(name, bars, WARMUP_BARS) for name in ("train", "validation")}
    feas_ranges = [(split.train.start_ts, split.train.end_ts), (split.validation.start_ts, split.validation.end_ts)]
    _CTX.clear()
    _CTX.update(bars=bars, rules=rules, risk=risk, segments=segments, feas_ranges=feas_ranges,
                sens_only=tuple(sens_only))

    tasks = [(sp, st, lab, str(fee)) for sp in ("train", "validation") for st in STRATEGIES
             for lab, fee, _ in FEE_SCENARIOS]
    t0 = time.time()
    grid = [r for part in _pool_map(_grid_task, tasks, workers) for r in part]
    print(f"[runner] grid: {len(grid)} runs in {time.time() - t0:.0f}s", file=sys.stderr)
    t0 = time.time()
    feas = dict(_pool_map(_feas_task, sorted(bars), workers))
    print(f"[runner] cost-gate feasibility scan in {time.time() - t0:.0f}s", file=sys.stderr)
    top_strategy, top_exit = choose_top_candidate(grid)
    fee0 = FEE_SCENARIOS[0][1]
    t0 = time.time()
    sens = _pool_map(_sens_task, [("train", top_exit, str(fee0)), ("validation", top_exit, str(fee0))], workers)
    print(f"[runner] sensitivity in {time.time() - t0:.0f}s", file=sys.stderr)
    bench = benchmark_rows(split, bars, rules)

    # full engine reports of the top candidate (train and validation), through backtest.report
    top_reports, top_full = {}, {}
    for name in ("train", "validation"):
        bars_seg, start_ts = segments[name]
        raw, view, eng = run_one(bars_seg, start_ts, rules, risk, top_strategy, top_exit, fee0, {})
        rep = build_report(view, title=f"Top candidate {top_strategy}+{top_exit} - {name} (EXPLORATORY)",
                           parameters={"strategy": top_strategy, "exit": top_exit, "taker_rate": fee0,
                                       "engine_result_hash": raw.result_hash,
                                       "engine_inputs_hash": raw.inputs_hash,
                                       "warmup_trimmed_equity_view": True, "split_note": SPLIT_NOTE,
                                       "momentum": text_fields(MomentumParams()),
                                       "exit_params": replace(ExitParams(), fee_rate=fee0),
                                       "risk": risk, "initial_equity": INITIAL_EQUITY},
                           split=name, limitations=_limitations(), repo_dir=Path(__file__).resolve().parent)
        top_full[name] = rep  # written below, after every git_sha lookup (so reports/ cannot make it "dirty")
        top_reports[name] = {"inputs_hash": rep["inputs_hash"], "result_hash": rep["result_hash"]}
    for name, rep in top_full.items():
        write_report(rep, out_dir, f"backtest_top_{name}")

    inputs = {"symbols": sorted(symbols), "split": split_summary(split),
              "bars_sha256": _bars_digest(bars), "rules": rules}
    report = {
        "title": "Backtest grid (train + validation only)", "status": EXPLORATORY,
        "git_head": git_head(),
        "inputs_hash": hashlib.sha256(json.dumps(to_canonical(inputs), sort_keys=True).encode()).hexdigest(),
        "inputs": {"symbols": sorted(symbols), "split": split_summary(split), "rules_source": rule_src,
                   "rules": {s: {"amount_precision": r.amount_precision, "price_precision": r.price_precision}
                             for s, r in sorted(rules.items())},
                   "data_quality": quality, "bars_sha256": inputs["bars_sha256"],
                   "initial_equity": str(INITIAL_EQUITY), "risk": to_canonical(risk),
                   "cooldown": to_canonical(RunnerCooldown()), "lookback_bars": LOOKBACK_BARS},
        "fee_scenarios": [{"label": lab, "taker_rate": str(fee), "note": note} for lab, fee, note in FEE_SCENARIOS],
        "grid": [{**{"strategy": s.strategy, "exit": s.exit, "fee_label": s.fee_label,
                     "taker_rate": str(s.taker_rate), "split": s.split}, **row} for s, row in grid],
        "benchmark_buy_and_hold": bench,
        "cost_gate_feasibility": {"note": ("fraction of 15m bars (train+validation segments only, features "
                                           "available) where the S1 cost gate passes; regime-dependent "
                                           "threshold (3x favourable, 4x tightened)"),
                                  "per_symbol": feas, "required_atr_pct": _required_table(),
                                  "atr_band": [MomentumParams().atr_pct_min, MomentumParams().atr_pct_max]},
        "top_candidate": {"strategy": top_strategy, "exit": top_exit, "fee_label": FEE_SCENARIOS[0][0],
                          "selection": "best S1 validation net return at the primary fee among variants with "
                                       "trades; ties/none -> pre-registered E4",
                          "reports": top_reports},
        "sensitivity": sens,
        "limitations": _limitations(),
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "backtest_grid.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    (out / "backtest_grid.md").write_text(render_grid_markdown(report))
    return report


def _required_table() -> dict:
    p = MomentumParams()
    return {lab: {"round_trip_cost_bps": float(CostModel(taker_rate=fee).market_context().round_trip_cost_bps),
                  "atr_pct_needed_favourable": required_atr_pct(p, CostModel(taker_rate=fee).market_context(),
                                                                favourable=True),
                  "atr_pct_needed_tightened": required_atr_pct(p, CostModel(taker_rate=fee).market_context(),
                                                               favourable=False)}
            for lab, fee, _ in FEE_SCENARIOS}


def _limitations() -> str:
    return (EXPLORATORY + " The universe was ranked by CURRENT 24h volume (selection bias). Window ~45 days. "
            "Engine limitations: fills at next-bar open/level touch with half-spread 7.5 bps and an impact proxy; "
            "fees are placeholders/assumptions; the cost gate is fed the engine's worst-case slippage cap "
            "(50 bps) plus the full spread, so the round-trip cost it sees is much larger than realised "
            "costs. Warm-up bars before each segment are excluded from metrics. Missing-minute bars are kept "
            "and counted. No parameters were tuned.")


def _bars_digest(bars: Mapping[str, Sequence[Candle]]) -> str:
    h = hashlib.sha256()
    for s in sorted(bars):
        h.update(s.encode())
        for c in bars[s]:
            h.update(f"{c.ts},{c.open},{c.high},{c.low},{c.close},{c.volume},{c.missing};".encode())
    return h.hexdigest()


# ------------------------------------------------------------------------------------------ markdown
def _f(v: Any, nd: int = 4) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}g}"
    return str(v)


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.2f}%"


def render_grid_markdown(r: Mapping[str, Any]) -> str:
    sp = r["inputs"]["split"]
    L = [f"# {r['title']}", "", f"**{r['status']}**", "",
         f"- git HEAD: `{r['git_head']}` ; inputs hash: `{r['inputs_hash']}`",
         f"- symbols: {', '.join(r['inputs']['symbols'])}", f"- split: {sp['note']}"]
    for name in ("train", "validation", "test"):
        s = sp[name]
        note = " (RESERVED, not evaluated)" if name == "test" else ""
        L.append(f"  - {name}: {s['start']} .. {s['end_last_bar']} ({s['bars']} bars){note}")
    L += ["", "Reading notes: a max drawdown near 8% is the RiskConfig soft-drawdown limit (`drawdown_soft_pct` 8): "
          "new entries are blocked after it, which is why many losing runs end at about -8%. Impact cost is "
          "mostly at the model's 50 bps cap because the liquidity proxy (previous 15m bar notional) is small "
          "next to 1000 USDT orders. Metrics exclude warm-up bars; 'bars' in the per-run reports counts the "
          "scored segment, the header's bar count includes warm-up.", "", "Fee scenarios:"]
    L += [f"- `{f['label']}` taker {f['taker_rate']}: {f['note']}" for f in r["fee_scenarios"]]
    for split in ("train", "validation"):
        L += ["", f"## {split.capitalize()} results", "",
              "| strategy | exit | fee | net return | max DD | trades | win rate | PF | exp (R) | Sharpe | "
              "signals/evals | fees | spread | impact | gross P&L |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for g in r["grid"]:
            if g["split"] != split:
                continue
            L.append(f"| {g['strategy']} | {g['exit']} | {g['fee_label']} | {_pct(g['net_return'])} | "
                     f"{_pct(g['max_drawdown'])} | {g['trade_count']} | {_pct(g['win_rate'])} | "
                     f"{_f(g['profit_factor'], 3)} | {_f(g['expectancy_r'], 3)} | {_f(g['sharpe'], 3)} | "
                     f"{g['signals']}/{g['evaluations']} | {g['fees']:.1f} | {g['spread_cost']:.1f} | "
                     f"{g['impact_cost']:.1f} | {g['gross_pnl']:.1f} |")
        L += ["", "Cost shares of |gross P&L| (fee / spread / impact) and abstentions:", "",
              "| strategy | exit | fee | fee share | spread share | impact share | abstentions | events |",
              "|---|---|---|---|---|---|---|---|"]
        for g in r["grid"]:
            if g["split"] != split:
                continue
            ev = ", ".join(f"{k}={v}" for k, v in g["events"].items()) or "-"
            L.append(f"| {g['strategy']} | {g['exit']} | {g['fee_label']} | {_f(g['fee_share_of_gross'], 3)} | "
                     f"{_f(g['spread_share_of_gross'], 3)} | {_f(g['slippage_share_of_gross'], 3)} | "
                     f"{g['abstentions']} | {ev} |")
    L += ["", "## Buy-and-hold benchmark (equal weight over the universe, same costs)", "",
          "| split | fee | mean net return | worst per-symbol max DD |", "|---|---|---|---|"]
    L += [f"| {b['split']} | {b['fee_label']} | {_pct(b['net_return'])} | {_pct(b['max_drawdown_worst_symbol'])} |"
          for b in r["benchmark_buy_and_hold"]]
    feas = r["cost_gate_feasibility"]
    L += ["", "## S1 cost-gate feasibility", "", feas["note"] + ".", "",
          "ATR/close needed for the gate to pass (S1 atr band is "
          f"[{feas['atr_band'][0]}, {feas['atr_band'][1]}]):", "",
          "| fee | round-trip cost (bps) | ATR% needed (favourable) | ATR% needed (tightened) |", "|---|---|---|---|"]
    L += [f"| {k} | {v['round_trip_cost_bps']:.0f} | {_pct(v['atr_pct_needed_favourable'])} | "
          f"{_pct(v['atr_pct_needed_tightened'])} |" for k, v in feas["required_atr_pct"].items()]
    labels = [f["label"] for f in r["fee_scenarios"]]
    L += ["", "Fraction of usable-feature bars where the cost gate passes (bars without usable features, e.g. too "
          "many zero-volume bars or a zero median volume, make S1 abstain regardless):", "",
          "| symbol | bars in train+val | usable-feature bars | median ATR% | p90 ATR% | max ATR% | "
          + " | ".join(labels) + " | other rules pass (no cost gate) |",
          "|---|---|---|---|---|---|" + "---|" * len(labels) + "---|"]
    for s, d in feas["per_symbol"].items():
        n = d["bars"] or 1
        L.append(f"| {s} | {d['bars_in_segments']} | {d['bars']} | {_pct(d['atr_pct_median'])} | "
                 f"{_pct(d['atr_pct_p90'])} | {_pct(d['atr_pct_max'])} | "
                 + " | ".join(f"{_pct(d['gate_pass'][k] / n)} ({d['gate_pass'][k]})" for k in labels)
                 + f" | {d['all_other_rules_pass_ignoring_cost_gate']} |")
    tc = r["top_candidate"]
    L += ["", f"## Top candidate: {tc['strategy']}+{tc['exit']} at {tc['fee_label']}", "", tc["selection"] + ".", ""]
    for sres in r["sensitivity"]:
        L.append(f"- sensitivity on {sres['split']} (+/-{sres['pct']}): base expectancy (R) "
                 f"{_f(sres['base_expectancy_r'], 3)}, same-sign fraction {_f(sres['fraction_same_sign'], 3)} over "
                 f"{len(sres['neighbours'])} neighbours")
    L += ["", "## Data quality (15m bars)", "", "| symbol | bars | bars with missing minutes | zero-volume bars | rules |",
          "|---|---|---|---|---|"]
    for s, q in r["inputs"]["data_quality"].items():
        rl = r["inputs"]["rules"][s]
        L.append(f"| {s} | {q['bars_15m']} | {q['bars_with_missing_minutes']} | {q['zero_volume_bars']} | "
                 f"{r['inputs']['rules_source'][s]}: amount {rl['amount_precision']}, price {rl['price_precision']} |")
    L += ["", "## Limitations", "", r["limitations"], ""]
    return "\n".join(L)


# ------------------------------------------------------------------------------------------ final test
class FrozenParamsError(ValueError):
    pass


def load_frozen_params(path: Path | str) -> dict:
    """Validate a frozen-parameter file: {"strategy": "S0|S1", "exit": "E1..E4", "taker_rate": "0.0035",
    "momentum_params": {..}, "exit_params": {..}}. Raises FrozenParamsError; consumes nothing."""
    p = Path(path)
    if not p.is_file():
        raise FrozenParamsError(f"params file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except ValueError as e:
        raise FrozenParamsError(f"params file is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise FrozenParamsError("params file must hold a JSON object")
    if raw.get("strategy") not in STRATEGIES or raw.get("exit") not in EXITS:
        raise FrozenParamsError(f"strategy must be one of {STRATEGIES} and exit one of {EXITS}")
    try:
        fee = Decimal(str(raw["taker_rate"]))
    except (KeyError, ArithmeticError) as e:
        raise FrozenParamsError("taker_rate is required (decimal text)") from e
    if not (0 <= fee < 1):
        raise FrozenParamsError("taker_rate out of range")
    mom = raw.get("momentum_params") or {}
    ex = raw.get("exit_params") or {}
    if raw["strategy"] == "S0" and mom:
        raise FrozenParamsError("momentum_params given for S0")
    try:
        mp = MomentumParams(**mom)
        xp = replace(ExitParams(), fee_rate=fee, **{k: (Decimal(str(v)) if k in _DEC_EXIT else v)
                                                    for k, v in ex.items()})
    except (TypeError, ValueError, ArithmeticError) as e:
        raise FrozenParamsError(f"bad parameters: {e}") from e
    return {"strategy": raw["strategy"], "exit": raw["exit"], "fee": fee, "momentum": mp, "exit_params": xp,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


_DEC_EXIT = {f.name for f in fields(ExitParams) if f.type in ("Decimal", Decimal)}


def run_final_test(history_dir: Path | str, out_dir: Path | str, params_file: Path | str | None, *,
                   ledger: Path | str = LEDGER_DEFAULT, symbols: Sequence[str] = DEFAULT_SYMBOLS,
                   markets_path: Path | str = FIXTURE_MARKETS) -> dict:
    """The ONE evaluation on the test segment. Refuses without a frozen params file; validates it, then
    consumes the TestSetGuard (a second call for the same test set raises TestSetAlreadyUsed) BEFORE running,
    so a crash cannot grant a second look. Writes backtest_final_test.{json,md}."""
    if params_file is None:
        raise FrozenParamsError("--final-test refuses to run without --params-file frozen.json")
    frozen = load_frozen_params(params_file)
    bars, quality = load_bars_15m(history_dir, symbols)
    split = build_split(bars)
    guard = TestSetGuard(ledger)
    test_id = make_test_set_id(split, symbols)
    guard.consume(test_id, note=f"final test; params sha256 {frozen['sha256']}")
    rules, _ = symbol_rules(symbols, bars, markets_path)
    risk = load_config(env={}).risk
    bars_seg, start_ts = split.bars_for("test", bars, WARMUP_BARS)
    raw, view, eng = run_one(bars_seg, start_ts, rules, risk, frozen["strategy"], frozen["exit"], frozen["fee"], {},
                             momentum=frozen["momentum"], exit_params=frozen["exit_params"])
    seg = split.test
    test_bars = {s: [c for c in cs if seg.start_ts <= c.ts <= seg.end_ts] for s, cs in sorted(bars.items())}
    bh = equal_weight_buy_and_hold(test_bars, INITIAL_EQUITY, CostModel(taker_rate=frozen["fee"],
                                                                        maker_rate=frozen["fee"]))
    rep = build_report(view, title=f"FINAL TEST {frozen['strategy']}+{frozen['exit']} (single use)",
                       parameters={"frozen_params_sha256": frozen["sha256"], "test_set_id": test_id,
                                   "strategy": frozen["strategy"], "exit": frozen["exit"],
                                   "taker_rate": frozen["fee"], "momentum": text_fields(frozen["momentum"]),
                                   "exit_params": frozen["exit_params"], "engine_result_hash": raw.result_hash,
                                   "evaluations": eng.evaluations, "signals": eng.signals,
                                   "abstentions": eng.evaluations - eng.signals, "events": _event_counts(raw),
                                   "buy_and_hold_net_return": repr(bh["net_return"]), "split": split_summary(split)},
                       split="test", limitations=_limitations(), repo_dir=Path(__file__).resolve().parent)
    write_report(rep, out_dir, "backtest_final_test")
    return rep
