"""Bar-by-bar, look-ahead-free backtest of a long-only spot strategy. Decimal for money/qty.

Depends only on the `SignalEngine` / `ExitPolicy` protocols of `strategy.contract` (plus an
optional, duck-typed `targets(state)` method on the exit policy returning
`(price, fraction, reason)` tuples), sizing, and the cost model.

Timeline: the sorted union of all symbols' bar open times. For each timestamp `ts`, in order:

A. OPEN of ts (per symbol with a bar at ts, symbols sorted):
   1. pending exits (decided at the previous close, incl. hard-breach flatten) fill at the open;
   2. pending entries fill at the open, best score first (ties: symbol). An entry is dropped if
      the symbol's previous bar is not the signal bar or is not exactly one interval earlier
      (data gap). Caps are checked here, at fill time: entry blocks (soft/hard breach, halt),
      one position per symbol, max_open_positions, cooldown hook, open <= stop, max exposure,
      then sizing (risk budget, max position, max order value, cash after fees, precision,
      min notional). Sizing is two-pass: estimate the fill without impact, size, cost the fill
      with impact, re-size at that (higher) price, cost again; impact is monotone in quantity so
      the final fill price is <= the price the final size was checked at.
B. INTRABAR of ts (each open position, incl. ones entered at this open):
   - open <= stop               -> whole position stopped at the OPEN (gap-through, worse price)
   - open >= target             -> that target fills at the TARGET price (never better)
   - low <= stop                -> whole position stopped at the stop. If the same bar also
                                   reached a target, the STOP is assumed first (conservative).
   - high >= target             -> target(s) fill at their price, ascending.
   Nothing else is known about the path inside a bar.
   Position state handed to `targets()` during the bar is the state at the OPEN: on the entry bar
   `highest_high_since_entry` is the entry bar's open (its high is unknown until the close).
C. CLOSE of ts: marks <- close; highest high and bars_held updated; equity marked to market
   (no liquidation cost) and appended to the curve; HWM/day loss/drawdown evaluated (RiskConfig
   percentages; trading day = UTC calendar day, start-of-day equity = equity at the previous
   day's last close). Hard breach -> flatten everything at the next open and halt entries for the
   rest of that UTC day (a hard drawdown persists while equity stays below, so it keeps blocking).
   Soft breach -> no new entries. Then the exit policy's decision for each open position, the
   engine time stop, and finally `signal_engine.evaluate` on the CLOSED bar for every symbol
   without a position; a signal is queued for the next bar's open.

Strategies and exit policies receive a TUPLE holding only the last `lookback_bars` bars up to and
including the bar that just closed (`bars_window`): no reference to the full series or to any
later bar exists in what they are handed, so no attribute or index can reach the future. The
default (400) covers S1's `MomentumParams.history_bars` (300, itself >= min_bars 120) and E1-E4's
indicator windows; `run_backtest` refuses a lookback shorter than the `history_bars` a component
declares (`component.params.history_bars` or `component.history_bars`). Building a window is a
bounded tuple slice, O(lookback) per call, never O(len(series)).

Price levels. Every stop/target the engine stores is on the symbol's price tick
(`price_precision`): the signal's initial stop is rounded DOWN (R never shrinks, so sizing is not
more aggressive than the strategy asked); a policy's `update_stop` is rounded UP (a long's stop
only gets tighter), except when rounding alone would lift it to or above the close it was decided
on (raw < close <= rounded), in which case it is rounded DOWN so a tick cannot trigger an exit the
policy did not ask for; stops still only ratchet up. Take-profit levels are rounded UP (the market
must reach at least the requested level; the fill is at that tick-valid level).

Money is exact: notional, fees, spread/impact attribution, cash, equity and P&L sums are computed
in `costs.EXACT` (1000 digits, Inexact trapped), so `net == gross - fees - spread - impact` holds
to the last digit for every valid input; the engine still asserts it. Only ratios (impact, breach
percentages, sizing divisions, r_multiple) round, in a 60-digit context.

End of data: open positions are sold at their last close with full costs and flagged
`end_of_data`; the final equity point is replaced by the post-liquidation value. The run is
deterministic and carries `inputs_hash` and `result_hash`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal, localcontext
from typing import Any, Callable, Mapping, Sequence

from wallexbot.backtest.costs import COST_MODEL_VERSION, EXACT, CostBook, CostModel, FillCost, round_up
from wallexbot.config import RiskConfig
from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import ExitDecision, ExitPolicy, PositionState, Signal, SignalEngine
from wallexbot.strategy.sizing import min_notional_for, size_position
from wallexbot.types import SymbolRules, canonical_decimal, round_down

ENGINE_VERSION = "bt-0.2"   # 0.2: tick-quantized stops/targets, exact money, open-only entry-bar state
_ZERO = Decimal(0)
_HUNDRED = Decimal(100)
_DAY = 86400
R_QUANTUM = Decimal("1e-12")  # r_multiple is a ratio (not money); fixed scale keeps hashes readable


@dataclass(frozen=True)
class BacktestParams:
    bar_interval_s: int = 900
    time_stop_bars: int | None = None        # engine-level time stop (policies may have their own)
    trade_start_ts: int | None = None        # signals on bars before this are ignored (warm-up)
    min_depth_multiple: Decimal = Decimal(5)  # used only if a depth proxy is ever supplied
    lookback_bars: int = 400                 # max bars handed to strategy/policy (>= their history_bars)


@dataclass(frozen=True)
class SimFill:
    ts: int
    symbol: str
    side: str          # BUY | SELL
    kind: str          # entry | stop | target | market | end_of_data
    reason: str
    ref_price: Decimal
    price: Decimal
    qty: Decimal
    notional: Decimal
    fee: Decimal
    spread_cost: Decimal
    impact_cost: Decimal


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_id: str
    signal_ts: int
    entry_ts: int
    entry_price: Decimal
    qty: Decimal               # initial quantity
    initial_stop: Decimal
    risk_per_unit: Decimal     # entry_price - initial_stop
    exit_ts: int
    exit_reason: str           # reason of the final exit leg
    exits: tuple[SimFill, ...]
    gross_pnl: Decimal         # frictionless: sum(q * exit_ref) - qty * entry_ref
    fees: Decimal
    spread_cost: Decimal
    impact_cost: Decimal
    net_pnl: Decimal           # gross - fees - spread - impact (exact)
    r_multiple: Decimal        # net_pnl / (qty * risk_per_unit)
    bars_held: int
    end_of_data: bool


@dataclass(frozen=True)
class EquityPoint:
    ts: int
    equity: Decimal
    cash: Decimal
    position_value: Decimal


@dataclass(frozen=True)
class Event:
    ts: int
    symbol: str
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[Trade, ...]
    equity_curve: tuple[EquityPoint, ...]
    fills: tuple[SimFill, ...]
    events: tuple[Event, ...]
    initial_equity: Decimal
    final_equity: Decimal
    stats_inputs: Mapping[str, Any]
    inputs_hash: str
    result_hash: str

    def compute_result_hash(self) -> str:
        return _result_hash(self.inputs_hash, self.trades, self.equity_curve, self.fills, self.events,
                            self.final_equity)


# ---------------------------------------------------------------- helpers
def bars_window(bars: tuple[Candle, ...], i: int, lookback: int) -> tuple[Candle, ...]:
    """The closed bars a strategy may see at bar index i: a NEW immutable tuple of at most
    `lookback` bars ending with bars[i]. It holds no reference to the series it was cut from."""
    return tuple(bars[max(0, i + 1 - lookback):i + 1])


def BarsView(bars: Sequence[Candle], end: int, start: int = 0) -> tuple[Candle, ...]:  # noqa: N802
    """Deprecated name kept for old callers: the tuple bars[start:end] (a copy, nothing beyond)."""
    return tuple(bars[start:end])


def declared_history_bars(component: Any) -> int:
    """Bars of history a strategy/exit component says it reads (`.params.history_bars` or
    `.history_bars`); 0 when it declares nothing."""
    for get in (lambda c: c.params.history_bars, lambda c: c.history_bars):
        try:
            value = get(component)
        except AttributeError:
            continue
        if value is not None:
            return int(value)
    return 0


def to_canonical(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return canonical_decimal(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return to_canonical(asdict(obj))
    if isinstance(obj, Mapping):
        return {str(k): to_canonical(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (list, tuple)):
        return [to_canonical(x) for x in obj]
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        raise TypeError("float in hashed backtest inputs; use Decimal")
    return repr(obj)


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(to_canonical(obj), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _component_id(obj: Any) -> Any:
    try:
        params = obj.params
    except AttributeError:
        params = None
    return {"type": f"{type(obj).__module__}.{type(obj).__qualname__}",
            "params": to_canonical(params) if params is not None and is_dataclass(params) else repr(params)}


def _result_hash(inputs_hash, trades, curve, fills, events, final_equity) -> str:
    return stable_hash([ENGINE_VERSION, inputs_hash, trades, curve, fills, events, final_equity])


def utc_day(ts: int) -> int:
    return ts // _DAY


def breach_state(risk: RiskConfig, equity: Decimal, sod_equity: Decimal, hwm: Decimal) -> tuple[list[str], list[str]]:
    """Same formula as RiskEngine.breaches (percent of SOD equity / HWM)."""
    daily = (sod_equity - equity) / sod_equity * _HUNDRED
    dd = (hwm - equity) / hwm * _HUNDRED
    soft, hard = [], []
    if daily >= risk.daily_loss_hard_pct:
        hard.append("daily_loss_hard")
    elif daily >= risk.daily_loss_soft_pct:
        soft.append("daily_loss_soft")
    if dd >= risk.drawdown_hard_pct:
        hard.append("drawdown_hard")
    elif dd >= risk.drawdown_soft_pct:
        soft.append("drawdown_soft")
    return soft, hard


Cooldown = Callable[[str, int, Sequence[Trade]], bool]  # True = entry blocked


@dataclass(frozen=True)
class LossStreakCooldown:
    """Block all entries for cooldown_s after the last of n_losses consecutive losing trades
    (RiskConfig.consecutive_loss_cooldown; the plan's 60 minutes = 3600 s)."""
    n_losses: int
    cooldown_s: int

    def __call__(self, symbol: str, ts: int, closed: Sequence[Trade]) -> bool:
        if self.n_losses <= 0 or len(closed) < self.n_losses:
            return False
        tail = closed[-self.n_losses:]
        return all(t.net_pnl < 0 for t in tail) and ts - tail[-1].exit_ts < self.cooldown_s


def loss_streak_cooldown(n_losses: int, cooldown_s: int) -> Cooldown:
    return LossStreakCooldown(n_losses, cooldown_s)


def _cooldown_id(cooldown: Any) -> Any:
    """Hashable identity of a cooldown hook: dataclass hooks by value, others by type/name only
    (their captured parameters are NOT covered by inputs_hash; prefer a frozen dataclass)."""
    if cooldown is None:
        return None
    if is_dataclass(cooldown):
        return {"type": type(cooldown).__qualname__, "params": to_canonical(cooldown)}
    try:
        return cooldown.__qualname__
    except AttributeError:
        return type(cooldown).__qualname__


# ---------------------------------------------------------------- simulation
@dataclass
class _Pos:
    symbol: str
    signal: Signal
    entry_ts: int
    entry_fill: SimFill
    qty: Decimal
    initial_stop: Decimal
    stop: Decimal
    highest_high: Decimal
    bars_held: int = 0
    partial_taken: bool = False
    targets_hit: set = field(default_factory=set)
    exits: list = field(default_factory=list)


class _Sim:
    def __init__(self, bars, signal_engine, exit_policy, cost_model, risk, rules, initial_equity, params,
                 cooldown):
        self.bars, self.se, self.xp, self.cm = bars, signal_engine, exit_policy, cost_model
        self.risk, self.rules, self.p, self.cooldown = risk, rules, params, cooldown
        self.symbols = sorted(bars)
        self.index = {s: {c.ts: i for i, c in enumerate(bars[s])} for s in self.symbols}
        self.initial = initial_equity
        self.cash = initial_equity
        self.positions: dict[str, _Pos] = {}
        self.pending_entries: dict[str, tuple[Signal, int]] = {}
        self.pending_exits: dict[str, tuple[Decimal | None, str]] = {}
        self.marks: dict[str, Decimal] = {}
        self.sod_day: int | None = None
        self.sod_equity = initial_equity
        self.hwm = initial_equity
        self.last_equity = initial_equity
        self.halt_day: int | None = None
        self.block_kinds: tuple[str, ...] = ()
        self.trades: list[Trade] = []
        self.fills: list[SimFill] = []
        self.events: list[Event] = []
        self.curve: list[EquityPoint] = []
        # per-symbol cost model (a CostBook) or one model for all; signal engines see that symbol's costs
        self.cms = {s: cost_model.for_symbol(s) for s in self.symbols}
        self.markets = {s: self.cms[s].market_context() for s in self.symbols}
        try:  # optional extension of the ExitPolicy protocol
            self.targets_fn = exit_policy.targets if callable(exit_policy.targets) else None
        except AttributeError:
            self.targets_fn = None

    # ---------------------------------------------------------- small utilities
    def event(self, ts, symbol, kind, detail=""):
        self.events.append(Event(ts, symbol, kind, detail))

    def view(self, s: str, i: int) -> tuple[Candle, ...]:
        return bars_window(self.bars[s], i, self.p.lookback_bars)

    def tick_down(self, s: str, price: Any) -> Decimal:
        return round_down(Decimal(price), self.rules[s].price_precision)

    def tick_up(self, s: str, price: Any) -> Decimal:
        return round_up(Decimal(price), self.rules[s].price_precision)

    def policy_stop(self, s: str, raw: Any, close: Decimal) -> Decimal:
        """A policy's stop on the tick: UP (tighter), unless rounding alone lifts it to the close."""
        raw = Decimal(raw)
        q = self.tick_up(s, raw)
        if raw < close <= q:
            q = self.tick_down(s, raw)
        return q

    def liquidity(self, s: str, i: int) -> Decimal | None:
        """Notional volume of the last bar CLOSED before bar i (never bar i itself)."""
        if i <= 0:
            return None
        b = self.bars[s][i - 1]
        return b.volume * b.close

    def state(self, pos: _Pos) -> PositionState:
        return PositionState(symbol=pos.symbol, entry_price=pos.entry_fill.price, entry_bar_ts=pos.entry_ts,
                             quantity=pos.qty, initial_stop=pos.initial_stop,
                             breakout_level=pos.signal.breakout_level, highest_high_since_entry=pos.highest_high,
                             bars_held=pos.bars_held, partial_taken=pos.partial_taken, current_stop=pos.stop)

    def blocked(self, ts: int) -> tuple[str, ...]:
        if self.halt_day is not None and self.halt_day == utc_day(ts):
            return ("halted_hard_breach",)
        soft, hard = breach_state(self.risk, self.last_equity, self.sod_equity, self.hwm)
        return tuple(hard + soft)

    # ---------------------------------------------------------- fills
    def _record(self, ts, s, side, kind, reason, ref, qty, cost: FillCost) -> SimFill:
        f = SimFill(ts, s, side, kind, reason, ref, cost.price, qty, cost.notional, cost.fee,
                    cost.spread_cost, cost.impact_cost)
        self.fills.append(f)
        return f

    def sell(self, ts: int, pos: _Pos, qty: Decimal, ref: Decimal, kind: str, reason: str,
             liquidity: Decimal | None) -> None:
        rules = self.rules[pos.symbol]
        remainder = pos.qty - qty
        if remainder > 0 and remainder * ref < min_notional_for(rules, self.risk):
            qty, reason = pos.qty, reason + "+dust"  # never leave an unsellable remainder
        cost = self.cms[pos.symbol].fill("SELL", ref, qty, liquidity, rules.price_precision)
        f = self._record(ts, pos.symbol, "SELL", kind, reason, ref, qty, cost)
        with localcontext(EXACT):
            self.cash += cost.notional - cost.fee
            pos.qty -= qty
        pos.exits.append(f)
        if pos.qty == 0:
            self._close(ts, pos, reason, end_of_data=(kind == "end_of_data"))

    def _close(self, ts: int, pos: _Pos, reason: str, end_of_data: bool) -> None:
        e = pos.entry_fill
        legs = tuple(pos.exits)
        with localcontext(EXACT):   # exact: a rounding here raises Inexact instead of hiding a residue
            gross = sum((x.qty * x.ref_price for x in legs), _ZERO) - e.qty * e.ref_price
            fees = e.fee + sum((x.fee for x in legs), _ZERO)
            spread = e.spread_cost + sum((x.spread_cost for x in legs), _ZERO)
            impact = e.impact_cost + sum((x.impact_cost for x in legs), _ZERO)
            net = sum((x.notional - x.fee for x in legs), _ZERO) - (e.notional + e.fee)
            if net != gross - fees - spread - impact:
                raise RuntimeError("cost attribution identity broken")
        rpu = e.price - pos.initial_stop
        self.trades.append(Trade(
            symbol=pos.symbol, signal_id=pos.signal.signal_id, signal_ts=pos.signal.bar_ts,
            entry_ts=pos.entry_ts, entry_price=e.price, qty=e.qty, initial_stop=pos.initial_stop,
            risk_per_unit=rpu, exit_ts=ts, exit_reason=reason, exits=legs, gross_pnl=gross, fees=fees,
            spread_cost=spread, impact_cost=impact, net_pnl=net,
            r_multiple=(net / (e.qty * rpu)).quantize(R_QUANTUM),
            bars_held=pos.bars_held, end_of_data=end_of_data))
        del self.positions[pos.symbol]
        self.pending_exits.pop(pos.symbol, None)

    # ---------------------------------------------------------- phase A
    def price_now(self, s: str, ts: int) -> Decimal:
        i = self.index[s].get(ts)
        return self.bars[s][i].open if i is not None else self.marks[s]

    def try_entry(self, ts: int, s: str, i: int, sig: Signal, sig_idx: int) -> None:
        bar = self.bars[s][i]
        if sig_idx != i - 1 or bar.ts - self.bars[s][sig_idx].ts != self.p.bar_interval_s:
            return self.event(ts, s, "entry_dropped", "data_gap")
        why = self.blocked(ts)
        if why:
            return self.event(ts, s, "entry_blocked", ",".join(why))
        if s in self.positions:
            return self.event(ts, s, "entry_rejected", "position_exists")
        if len(self.positions) >= self.risk.max_open_positions:
            return self.event(ts, s, "entry_rejected", "max_open_positions")
        if self.cooldown is not None and self.cooldown(s, ts, tuple(self.trades)):
            return self.event(ts, s, "entry_rejected", "cooldown")
        stop = self.tick_down(s, sig.stop_price)   # on the tick; DOWN keeps R >= the strategy's R
        if stop <= 0:
            return self.event(ts, s, "entry_rejected", "stop_below_one_tick")
        if bar.open <= stop:
            return self.event(ts, s, "entry_rejected", "open_at_or_below_stop")
        with localcontext(EXACT):
            held = sum((p.qty * self.price_now(sym, ts) for sym, p in self.positions.items()), _ZERO)
            equity = self.cash + held
        headroom = equity * self.risk.max_exposure_pct / _HUNDRED - held
        rules, liq, cm = self.rules[s], self.liquidity(s, i), self.cms[s]

        def size(px: Decimal):
            return size_position(equity, px, stop, rules, self.risk, self.cash, cm.taker_rate,
                                 None, sig.score, min_depth_multiple=self.p.min_depth_multiple,
                                 exposure_headroom=headroom)

        r1 = size(round_up(bar.open * (1 + cm.half_spread), rules.price_precision))  # no impact yet
        if not r1.ok:
            return self.event(ts, s, "entry_rejected", ",".join(r1.reasons))
        c1 = cm.fill("BUY", bar.open, r1.qty, liq, rules.price_precision)
        r2 = size(c1.price)
        if not r2.ok:
            return self.event(ts, s, "entry_rejected", ",".join(r2.reasons))
        c2 = cm.fill("BUY", bar.open, r2.qty, liq, rules.price_precision)
        if c2.price > c1.price or c2.notional + c2.fee > self.cash:
            raise RuntimeError("sizing monotonicity broken")  # cannot happen: impact is monotone in qty
        if c2.price <= stop:
            return self.event(ts, s, "entry_rejected", "fill_at_or_below_stop")
        f = self._record(ts, s, "BUY", "entry", "signal", bar.open, r2.qty, c2)
        with localcontext(EXACT):
            self.cash -= c2.notional + c2.fee
        # At the open only the open is known: the entry bar's high is folded in at its close.
        self.positions[s] = _Pos(symbol=s, signal=sig, entry_ts=ts, entry_fill=f, qty=r2.qty,
                                 initial_stop=stop, stop=stop, highest_high=bar.open)
        if r2.reasons:
            self.event(ts, s, "entry_sized", ",".join(r2.reasons))

    # ---------------------------------------------------------- phase B
    def intrabar(self, ts: int, s: str, i: int) -> None:
        pos, bar = self.positions[s], self.bars[s][i]
        liq = self.liquidity(s, i)
        if bar.open <= pos.stop:
            return self.sell(ts, pos, pos.qty, bar.open, "stop", "stop_gap", liq)
        targets = []
        if self.targets_fn is not None:
            targets = sorted(((self.tick_up(s, p), Decimal(f), str(r)) for p, f, r in self.targets_fn(self.state(pos))
                              if str(r) not in pos.targets_hit), key=lambda t: (t[0], t[2]))
        for price, frac, reason in [t for t in targets if bar.open >= t[0]]:
            self.take(ts, pos, price, frac, reason, liq)
            targets.remove((price, frac, reason))
            if s not in self.positions:
                return
        if bar.low <= pos.stop:
            touched = any(bar.high >= t[0] for t in targets)
            return self.sell(ts, pos, pos.qty, pos.stop, "stop", "stop_ambiguous_stop_first" if touched else "stop", liq)
        for price, frac, reason in targets:
            if bar.high >= price:
                self.take(ts, pos, price, frac, reason, liq)
                if s not in self.positions:
                    return

    def take(self, ts, pos: _Pos, price, frac, reason, liq) -> None:
        pos.targets_hit.add(reason)
        qty = pos.qty if frac >= 1 else round_down(pos.qty * frac, self.rules[pos.symbol].amount_precision)
        if qty <= 0:
            return self.event(ts, pos.symbol, "target_skipped", f"{reason}:zero_qty")
        if qty < pos.qty:
            pos.partial_taken = True
        self.sell(ts, pos, qty, price, "target", reason, liq)

    # ---------------------------------------------------------- phase C
    def apply_decision(self, ts: int, pos: _Pos, d: ExitDecision, close: Decimal) -> None:
        if d.action == "hold":
            return
        if d.action == "exit_all":
            self.pending_exits[pos.symbol] = (None, d.reason or "exit_all")
        elif d.action == "exit_partial":
            if not (0 < d.fraction < 1):
                raise ValueError(f"exit_partial fraction out of range: {d.fraction}")
            self.pending_exits[pos.symbol] = (d.fraction, d.reason or "exit_partial")
        elif d.action == "update_stop":
            if d.new_stop is not None:
                new = self.policy_stop(pos.symbol, d.new_stop, close)
                if new > pos.stop:  # ratchet up only
                    pos.stop = new
        else:
            raise ValueError(f"unknown exit action {d.action!r}")

    def step(self, ts: int) -> None:
        day = utc_day(ts)
        if self.sod_day is None:
            self.sod_day = day
        elif day != self.sod_day:
            self.sod_day, self.sod_equity = day, self.last_equity
        active = [(s, self.index[s][ts]) for s in self.symbols if ts in self.index[s]]

        # A. open
        for s, i in active:
            if s in self.pending_exits and s in self.positions:
                frac, reason = self.pending_exits.pop(s)
                pos = self.positions[s]
                qty = pos.qty if frac is None else round_down(pos.qty * frac, self.rules[s].amount_precision)
                if qty <= 0:
                    self.event(ts, s, "exit_skipped", f"{reason}:zero_qty")
                    continue
                if frac is not None:
                    pos.partial_taken = True
                self.sell(ts, pos, qty, self.bars[s][i].open, "market", reason, self.liquidity(s, i))
        entries = [(self.pending_entries.pop(s), s, i) for s, i in active if s in self.pending_entries]
        for (sig, sig_idx), s, i in sorted(entries, key=lambda e: (-e[0][0].score, e[1])):
            self.try_entry(ts, s, i, sig, sig_idx)

        # B. intrabar
        for s, i in active:
            if s in self.positions:
                self.intrabar(ts, s, i)

        # C. close
        for s, i in active:
            bar = self.bars[s][i]
            self.marks[s] = bar.close
            pos = self.positions.get(s)
            if pos is not None:
                pos.highest_high = max(pos.highest_high, bar.high)
                pos.bars_held += 1
        with localcontext(EXACT):
            pv = sum((p.qty * self.marks[s] for s, p in self.positions.items()), _ZERO)
            equity = self.cash + pv
        self.curve.append(EquityPoint(ts, equity, self.cash, pv))
        self.last_equity = equity
        self.hwm = max(self.hwm, equity)
        soft, hard = breach_state(self.risk, equity, self.sod_equity, self.hwm)
        kinds = tuple(hard + soft)
        if kinds != self.block_kinds:
            self.event(ts, "", "breach_state", ",".join(kinds) or "clear")
            self.block_kinds = kinds
        if hard:
            self.halt_day = day
            if self.pending_entries:
                self.pending_entries.clear()
            for s in sorted(self.positions):
                if self.pending_exits.get(s, (None, ""))[1] != "hard_breach_flatten":
                    self.pending_exits[s] = (None, "hard_breach_flatten")
                    self.event(ts, s, "flatten_scheduled", ",".join(hard))
        for s, i in active:
            pos = self.positions.get(s)
            if pos is None or s in self.pending_exits:
                continue
            self.apply_decision(ts, pos, self.xp.on_bar(self.state(pos), self.view(s, i)), self.bars[s][i].close)
            tsb = self.p.time_stop_bars
            if tsb is not None and pos.bars_held >= tsb and s not in self.pending_exits:
                self.pending_exits[s] = (None, "engine_time_stop")
        for s, i in active:
            if s in self.positions or s in self.pending_exits:
                continue
            bar = self.bars[s][i]
            sig = self.se.evaluate(s, self.view(s, i), self.markets[s])
            if sig is None:
                continue
            if sig.symbol != s or sig.bar_ts != bar.ts:
                raise ValueError(f"signal for {sig.symbol}@{sig.bar_ts} returned on {s}@{bar.ts}: contract violation")
            if self.p.trade_start_ts is not None and ts < self.p.trade_start_ts:
                continue
            if not (0 < sig.stop_price < sig.entry_ref_price):
                self.event(ts, s, "signal_rejected", "stop_not_below_entry")
                continue
            why = self.blocked(ts + self.p.bar_interval_s)
            if why:
                self.event(ts, s, "signal_blocked", ",".join(why))
                continue
            self.pending_entries[s] = (sig, i)

    def finish(self) -> None:
        if not self.curve:
            return
        last_ts = self.curve[-1].ts
        for s in sorted(self.positions):
            i = len(self.bars[s]) - 1
            b = self.bars[s][i]
            self.sell(last_ts, self.positions[s], self.positions[s].qty, b.close, "end_of_data", "end_of_data",
                      b.volume * b.close)
        for s in sorted(self.pending_entries):
            self.event(last_ts, s, "entry_dropped", "end_of_data")
        self.pending_entries.clear()
        self.curve[-1] = EquityPoint(last_ts, self.cash, self.cash, _ZERO)


def _validate(bars_by_symbol, rules, initial_equity, params) -> dict[str, list[Candle]]:
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    if params.bar_interval_s <= 0:
        raise ValueError("bar_interval_s must be positive")
    if not isinstance(params.lookback_bars, int) or params.lookback_bars < 1:
        raise ValueError("lookback_bars must be a positive int")
    out = {}
    for s in sorted(bars_by_symbol):
        bs = tuple(bars_by_symbol[s])
        if s not in rules:
            raise ValueError(f"no SymbolRules for {s}")
        if any(b.ts <= a.ts for a, b in zip(bs, bs[1:])):
            raise ValueError(f"bars for {s} are not strictly increasing")
        if any(b.ts % params.bar_interval_s for b in bs):
            raise ValueError(f"bars for {s} are not aligned to the {params.bar_interval_s}s grid")
        if bs:
            out[s] = bs
    return out


def inputs_hash(bars_by_symbol, signal_engine, exit_policy, cost_model, risk, rules, initial_equity, params,
                cooldown=None) -> str:
    return stable_hash({
        "engine": ENGINE_VERSION, "cost_model_version": COST_MODEL_VERSION,
        "bars": {s: [[c.ts, c.open, c.high, c.low, c.close, c.volume, c.missing] for c in bars_by_symbol[s]]
                 for s in sorted(bars_by_symbol)},
        "signal_engine": _component_id(signal_engine), "exit_policy": _component_id(exit_policy),
        "cost_model": cost_model, "risk": risk, "rules": {s: rules[s] for s in sorted(bars_by_symbol)},
        "initial_equity": initial_equity, "params": params,
        "cooldown": _cooldown_id(cooldown),
    })


def run_backtest(bars_by_symbol: Mapping[str, Sequence[Candle]], signal_engine: SignalEngine,
                 exit_policy: ExitPolicy, cost_model: CostModel | CostBook, risk: RiskConfig,
                 rules: Mapping[str, SymbolRules], initial_equity: Decimal,
                 params: BacktestParams | None = None, *, cooldown: Cooldown | None = None) -> BacktestResult:
    params = params or BacktestParams()
    need = max(declared_history_bars(signal_engine), declared_history_bars(exit_policy))
    if isinstance(params.lookback_bars, int) and params.lookback_bars < need:
        raise ValueError(f"lookback_bars {params.lookback_bars} < the {need} bars of history a component reads")
    with localcontext() as ctx:
        ctx.prec = 60
        bars = _validate(bars_by_symbol, rules, initial_equity, params)
        ih = inputs_hash(bars, signal_engine, exit_policy, cost_model, risk, rules, initial_equity, params, cooldown)
        sim = _Sim(bars, signal_engine, exit_policy, cost_model, risk, rules, initial_equity, params, cooldown)
        timeline = sorted({c.ts for bs in bars.values() for c in bs})
        for ts in timeline:
            sim.step(ts)
        sim.finish()
        trades, curve, fills, events = tuple(sim.trades), tuple(sim.curve), tuple(sim.fills), tuple(sim.events)
        final = curve[-1].equity if curve else initial_equity
        stats = {"bar_interval_s": params.bar_interval_s, "n_bars": len(timeline),
                 "start_ts": timeline[0] if timeline else None, "end_ts": timeline[-1] if timeline else None,
                 "symbols": sorted(bars), "engine_version": ENGINE_VERSION}
        rh = _result_hash(ih, trades, curve, fills, events, final)
    return BacktestResult(trades, curve, fills, events, initial_equity, final, stats, ih, rh)
