"""The autonomous trading loop (mock / shadow modes). `Bot.tick()` is called every poll (10-15 s).

Invariants:
- Single order path. Every order goes RiskEngine -> OMS.submit through pipeline.route_to_oms;
  this package never touches the broker's order entry (tests/test_bot_ast.py). Broker READS
  (balances) and cancels of resting orders (pipeline.cancel_open_intent, OMS.cancel_stale) only.
- No duplicate orders. Entry: the plan (keyed by the deterministic signal_id) is persisted BEFORE
  the intent is handed to the OMS, and a signal_id that has a plan is never planned again; the
  OMS refuses a client_id it has seen. Exit: no new exit for a symbol while ANY non-terminal SELL
  intent exists for it (the bot's or the guardian's); exit signal_ids are x:<position key>:<n>
  with n = durable count of earlier exit intents for the position.
- Stops survive restarts: position state (stops, highest high, bars held, partial flag, pending
  exit request) and the cooldown tracker are appended to the store on every change (bot/state.py)
  and reloaded on startup, then reconciled with the OMS ledger and exchange balances.
- Never raises. Each stage is isolated; a failing stage is recorded (errors table) and the tick
  goes on. `max_consecutive_failures` failing ticks in a row engage the kill switch (entries off,
  exits continue). A tick with any failed stage takes no new entries.
- Entries only on a NEW closed 15m bar, and only with fresh data (quote age <= max_data_age_s,
  bars lagging the clock by <= max_bar_lag_bars). Exits: strategy decisions on new bars, software
  stop / take-profit checks on every poll, stale data or not.

Per tick: heartbeat -> (startup) -> feed refresh -> reconcile, cancel stale entry orders (emulated
IOC, 20 s) and stale LIMIT exits (10 s) -> sync positions with the ledger -> new-bar work (exit
policy per position, entry evaluation / ranking / sizing / advisor / submit) -> software stops and
targets on live quotes -> place pending exits -> sync again.

Run the guardian as a SEPARATE process on the same database:
    python -m wallexbot.guardian.main --db PATH --mode mock|shadow
It watches the "strategy" heartbeat written here every tick, loss limits and ledger-vs-exchange.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, Decimal
from typing import Any, Callable, Mapping, Sequence

from wallexbot.bot.advice import AdvisorGate
from wallexbot.bot.feed import BAR_S, notional_within
from wallexbot.bot.state import (ACTOR, EXIT_PREFIX, BotState, EntryPlan, ExitRequest, TrackedPosition,
                                 exit_prefix, exit_signal_id)
from wallexbot.config import Config
from wallexbot.data.candles import Candle
from wallexbot.pipeline import cancel_open_intent, route_to_oms, snapshot_account
from wallexbot.risk.engine import AccountState, utc_day
from wallexbot.strategy.config import MomentumParams
from wallexbot.strategy.cooldown import CooldownTracker
from wallexbot.strategy.sizing import min_notional_for
from wallexbot.types import TERMINAL_STATES, IntentState, OrderIntent, OrderType, Position, Quote, Side, round_down

S = IntentState
GUARDIAN_PREFIX = "gflat:"          # guardian flatten intents (guardian/flatten.py), never recovered here
HEARTBEAT_COMPONENT = "strategy"    # what the guardian watches
_ALL_STATES = frozenset(IntentState)
_RETRY_REASONS = frozenset({"tick_degraded", "stale_feed", "no_market_context"})
_ZERO = Decimal(0)
_BPS = Decimal(10000)
_HUNDRED = Decimal(100)
log = logging.getLogger("wallexbot.bot")


@dataclass(frozen=True)
class BotParams:
    bar_s: int = BAR_S
    history_bars: int = MomentumParams.history_bars
    entry_tolerance_bps: Decimal = Decimal("10")       # entry LIMIT <= ask * (1 + 10 bps); capped at 10
    entry_ioc_s: float = 20.0                           # emulated IOC: unfilled entry canceled after 20 s
    exit_limit_tolerance_bps: Decimal = Decimal("20")  # LIMIT exit at bid * (1 - 20 bps), as the guardian
    exit_limit_timeout_s: float = 10.0                  # then canceled; the next attempt is MARKET
    max_exit_failures: int = 3                          # zero-fill exit attempts per request -> needs_review
    max_exit_attempts: int = 10                         # any exit attempts per request -> needs_review
    invalidation_atr_mult: Decimal = Decimal("0.5")     # no entry if ask > signal close + 0.5 ATR
    max_signal_age_bars: int = 2                        # no entry once 2 bars closed after the signal bar
    max_bar_lag_bars: int = 2                           # newest bar older than this vs the clock: stale
    orphan_stop_atr_mult: Decimal = Decimal("1.5")      # orphan stop = entry - 1.5 ATR
    orphan_atr_period: int = 14
    orphan_fallback_stop_pct: Decimal = Decimal("5")    # no ATR yet: entry - 5 %, needs_review, exit-only
    max_consecutive_failures: int = 5
    advisor_timeout_s: float = 2.0
    cooldown_bars: int = MomentumParams.cooldown_bars
    cooldown_bars_profitable: int = MomentumParams.cooldown_bars_profitable

    def __post_init__(self) -> None:
        if not 0 <= self.entry_tolerance_bps <= 10:
            raise ValueError("entry_tolerance_bps must be in [0, 10]")
        if not 0 <= self.exit_limit_tolerance_bps < 10000:
            raise ValueError("exit_limit_tolerance_bps must be in [0, 10000)")
        if self.entry_ioc_s <= 0 or self.exit_limit_timeout_s <= 0 or self.advisor_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        if min(self.max_consecutive_failures, self.max_exit_failures, self.max_exit_attempts,
               self.history_bars, self.orphan_atr_period) < 1 or self.bar_s <= 0:
            raise ValueError("counts and periods must be >= 1")


@dataclass
class TickReport:
    ts: float
    new_bar: bool = False
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    engaged_kill_switch: bool = False


def simple_atr(bars: Sequence[Candle], period: int) -> Decimal | None:
    """Mean true range of the last `period` bars (orphan stops only)."""
    if len(bars) < period + 1:
        return None
    w = list(bars[-(period + 1):])
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(w, w[1:])]
    return sum(trs, _ZERO) / period


class Bot:
    def __init__(self, feed, signal_engine, exit_policy, sizing: Callable[..., Any], oms, risk, store, killswitch,
                 config: Config, clock: Callable[[], float] = time.time, advisor: Any = None, *,
                 params: BotParams | None = None) -> None:
        self.feed, self.engine, self.exit_policy, self.sizing = feed, signal_engine, exit_policy, sizing
        self.oms, self.risk, self.store, self.ks, self.cfg, self.clock = oms, risk, store, killswitch, config, clock
        self.p = params or BotParams()
        self.state = BotState(store)
        self.advisor = AdvisorGate(advisor, self.p.advisor_timeout_s) if advisor is not None else None
        self.cooldown = CooldownTracker(self.p.bar_s, self.p.cooldown_bars, self.p.cooldown_bars_profitable)
        self.positions: dict[str, TrackedPosition] = {}
        self.plans: dict[str, EntryPlan] = {}
        self.planned: set[str] = set()
        self.bars_done: dict[str, int] = {}
        self.started = False
        self.consecutive_failures = 0
        self.ticks = 0
        self._stale_noted: dict[str, int] = {}
        self._retry_noted: set[tuple[str, int, str]] = set()

    # ================================================================== tick
    def tick(self) -> TickReport:
        """One poll. Never raises (Exception); see the module docstring for the stages."""
        try:
            now = float(self.clock())
        except Exception:
            now = time.time()
        rep = TickReport(now)
        try:
            self._run(now, rep)
        except Exception as e:  # defensive: stages are already isolated
            rep.errors.append(f"tick:{type(e).__name__}")
        try:
            self._count_failures(now, rep)
        except Exception as e:
            rep.errors.append(f"failures:{type(e).__name__}")
        self.ticks += 1
        return rep

    def _run(self, now: float, rep: TickReport) -> None:
        self._stage("heartbeat", self._heartbeat, now, rep)
        if not self.started:
            self._stage("startup", self._startup, now, rep)
            if not self.started:
                return  # never trade without the persisted state
        for name, fn in (("feed", self._refresh_feed), ("reconcile", self._reconcile), ("sync", self._sync),
                         ("bars", self._on_bars), ("protect", self._protect), ("exits", self._work_exits),
                         ("sync", self._sync)):
            self._stage(name, fn, now, rep)

    def _stage(self, name: str, fn: Callable[[float, TickReport], None], now: float, rep: TickReport) -> None:
        try:
            fn(now, rep)
        except Exception as e:
            rep.errors.append(f"{name}:{type(e).__name__}")
            log.exception("bot: stage %s failed", name)
            self._error(name, repr(e), now)

    def _count_failures(self, now: float, rep: TickReport) -> None:
        self.consecutive_failures = self.consecutive_failures + 1 if rep.errors else 0
        rep.consecutive_failures = self.consecutive_failures
        if self.consecutive_failures >= self.p.max_consecutive_failures and not self.ks.is_engaged():
            self.ks.engage(f"bot: {self.consecutive_failures} consecutive tick failures", actor=ACTOR, ts=now)
            self._decision("engage_kill_switch", {"consecutive_failures": self.consecutive_failures,
                                                  "errors": rep.errors}, now)
            rep.engaged_kill_switch = True

    # ================================================================== stages
    def _heartbeat(self, now: float, rep: TickReport) -> None:
        self.store.heartbeat(HEARTBEAT_COMPONENT, ts=now)

    def _startup(self, now: float, rep: TickReport) -> None:
        loaded = self.state.load()
        cooldown = CooldownTracker.from_dict(loaded.cooldown) if loaded.cooldown else self.cooldown
        recovered = self._recover_own_intents(now)
        self.oms.reconcile(now)
        self.positions, self.plans, self.planned = loaded.positions, loaded.open_plans, loaded.planned
        self.bars_done, self.cooldown = loaded.bars_done, cooldown
        self.started = True
        self._decision("startup", {"positions": {s: {"key": t.key, "current_stop": t.current_stop}
                                                 for s, t in sorted(self.positions.items())},
                                   "open_plans": sorted(self.plans), "recovered": recovered}, now)
        rep.actions.append("startup")

    def _recover_own_intents(self, now: float) -> list[str]:
        """Settle THIS loop's intents interrupted mid-submit by a previous run (PROPOSED never reached
        the broker -> rejected; RISK_APPROVED may have -> unknown_pending, resolved by reconcile).
        The guardian's own intents are left to the guardian."""
        done = []
        for rec in self.store.intents_in_states({S.PROPOSED, S.RISK_APPROVED}):
            if rec.intent.signal_id.startswith(GUARDIAN_PREFIX):
                continue
            new = S.REJECTED if rec.state is S.PROPOSED else S.UNKNOWN_PENDING
            try:
                self.store.set_intent_state(rec.intent.client_id, new, expected=rec.state,
                                            reason="bot_recover", ts=now)
                done.append(rec.intent.client_id)
            except Exception as e:
                self._error("recover", repr(e), now)
        return done

    def _refresh_feed(self, now: float, rep: TickReport) -> None:
        self.feed.track(sorted(set(self.positions) | {p.symbol for p in self.plans.values()}))
        self.feed.refresh(now)

    def _reconcile(self, now: float, rep: TickReport) -> None:
        self.oms.reconcile(now)
        for cid in self.oms.cancel_stale(self.p.entry_ioc_s, now):
            self._decision("entry_ioc_canceled", {"client_id": cid}, now)
            rep.actions.append("entry_ioc_canceled")
        for rec in self.store.intents_in_states({S.SUBMITTED, S.PARTIALLY_FILLED}):
            i = rec.intent
            if (i.reduce_only and i.type is OrderType.LIMIT and i.signal_id.startswith(EXIT_PREFIX + ":")
                    and now - rec.created_at > self.p.exit_limit_timeout_s):
                st = cancel_open_intent(self.oms, i.client_id, now)
                self._decision("exit_limit_timeout", {"client_id": i.client_id, "symbol": i.symbol,
                                                      "state": st.value if st else None}, now)
                rep.actions.append("exit_limit_canceled")

    def _sync(self, now: float, rep: TickReport) -> None:
        """Ledger -> tracked positions: adopt filled entries, adopt orphans, close flat positions."""
        ledger = self.oms.positions()
        for sid, plan in sorted(self.plans.items()):
            rec = self.store.get_intent(plan.client_id)
            pos = ledger.get(plan.symbol)
            if plan.symbol not in self.positions and pos is not None and not self._is_dust(plan.symbol, pos):
                self._adopt(plan, pos, now, rep)
            if rec is None or rec.state in TERMINAL_STATES:
                self.state.resolve_plan(plan, "never_submitted" if rec is None else rec.state.value, now)
                del self.plans[sid]
        planned_syms = {p.symbol for p in self.plans.values()}
        for sym, pos in sorted(ledger.items()):
            if sym not in self.positions and sym not in planned_syms and not self._is_dust(sym, pos):
                self._adopt_orphan(sym, pos, now, rep)
        for sym, tp in sorted(self.positions.items()):
            pos = ledger.get(sym, Position(sym))
            if self._is_dust(sym, pos) and not self._sells_in_flight(sym):
                self._close(tp, pos, now, rep)
        self._check_exchange(ledger, now)

    def _on_bars(self, now: float, rep: TickReport) -> None:
        B = self.p.bar_s
        boundary = int(now) // B * B - B            # open time of the newest bar closed by now
        universe = set(self.feed.symbols())
        block = self._entry_block(rep)
        self._retry_noted = {k for k in self._retry_noted if k[1] >= boundary - 4 * B}
        done: dict[str, int] = {}
        candidates = []
        for sym in sorted(universe | set(self.positions)):
            bars = self.feed.closed_bars(sym, self.p.history_bars)
            if not bars or bars[-1].ts <= self.bars_done.get(sym, -1):
                continue
            if (boundary - bars[-1].ts) // B > self.p.max_bar_lag_bars:
                if self._stale_noted.get(sym) != boundary:
                    self._stale_noted[sym] = boundary
                    self._decision("abstain", {"symbol": sym, "bar_ts": bars[-1].ts, "reason": "bars_stale"}, now)
                continue
            try:
                if sym in self.positions:
                    self._manage(sym, bars, now, rep)
                elif sym in universe:
                    sig, retry = self._evaluate(sym, bars, now, block)
                    if sig is not None:
                        candidates.append(sig)
                    if retry:   # transient data problem: evaluate this bar again on the next poll
                        continue
                done[sym] = bars[-1].ts
            except Exception as e:  # one symbol's failure must not stop the others (retried next tick)
                rep.errors.append(f"bars:{sym}:{type(e).__name__}")
                self._error("bars", f"{sym}: {e!r}", now)
        for sig in sorted(candidates, key=lambda s: (-s.score, s.symbol)):
            try:
                self._enter(sig, now, rep)
            except Exception as e:
                rep.errors.append(f"entry:{sig.symbol}:{type(e).__name__}")
                self._error("entry", f"{sig.symbol}: {e!r}", now)
        if done:
            rep.new_bar = True
            self.bars_done.update(done)
            self.state.mark_bars(done, now)

    def _protect(self, now: float, rep: TickReport) -> None:
        """Software stop and take-profit levels on the latest quote, every poll."""
        ledger = self.oms.positions()
        for sym, tp in sorted(self.positions.items()):
            try:
                self._protect_one(sym, tp, ledger, now, rep)
            except Exception as e:  # isolate symbols
                rep.errors.append(f"protect:{sym}:{type(e).__name__}")
                self._error("protect", f"{sym}: {e!r}", now)

    def _protect_one(self, sym: str, tp: TrackedPosition, ledger: Mapping[str, Position], now: float,
                     rep: TickReport) -> None:
        if tp.exit_request is not None and tp.exit_request.target_qty == 0:
            return
        q = self.feed.quote(sym)
        if q is None:
            return
        new = tp
        if q.bid <= tp.current_stop:
            new = self._request(tp, _ZERO, "stop", now, stop_hit=True, bid=q.bid)
        elif not tp.needs_review:
            qty = ledger.get(sym, Position(sym)).quantity
            for t in self._targets(tp, qty):
                if q.bid < t.price:
                    continue
                if t.fraction >= 1:
                    new = self._request(tp, _ZERO, t.reason, now, bid=q.bid)
                elif not tp.partial_taken and tp.exit_request is None:
                    new = self._request(tp, qty - self._step_down(sym, qty * t.fraction), t.reason, now, bid=q.bid)
                break
        if new is not tp:
            self._save(new, now)
            rep.actions.append(f"exit_request:{sym}")

    def _work_exits(self, now: float, rep: TickReport) -> None:
        for sym, tp in sorted(self.positions.items()):
            if tp.exit_request is not None:
                try:
                    self._place_exit(tp, now, rep)
                except Exception as e:  # isolate symbols; retried next tick
                    rep.errors.append(f"exits:{sym}:{type(e).__name__}")
                    self._error("exits", f"{sym}: {e!r}", now)

    # ================================================================== entries
    def _entry_block(self, rep: TickReport) -> str | None:
        if self.ks.is_engaged():
            return "kill_switch"
        if rep.errors:
            return "tick_degraded"
        return None

    def _evaluate(self, sym: str, bars: Sequence[Candle], now: float, block: str | None) -> tuple[Any, bool]:
        """(signal or None, retry). retry: the abstention was transient (stale data, degraded tick), so
        the bar stays unprocessed and is evaluated again next poll (logged once per bar and reason)."""
        bar_ts = bars[-1].ts

        def abstain(reason: str, **extra: Any) -> tuple[None, bool]:
            retry = reason in _RETRY_REASONS
            if not retry or (sym, bar_ts, reason) not in self._retry_noted:
                self._decision("abstain", {"symbol": sym, "bar_ts": bar_ts, "reason": reason, **extra}, now)
            if retry:
                self._retry_noted.add((sym, bar_ts, reason))
            return None, retry

        if block:
            return abstain(block)
        if sym not in self.risk.rules:
            return abstain("unknown_symbol")
        if self._symbol_busy(sym):
            return abstain("open_order_or_position")
        q = self.feed.quote(sym)
        if not self._fresh(q, now):
            return abstain("stale_feed")
        mc = self.feed.market_context(sym)
        if mc is None:
            return abstain("no_market_context")
        try:
            sig = self.engine.evaluate(sym, bars, mc)
        except Exception as e:
            return abstain(f"engine_error:{type(e).__name__}")
        if sig is None:
            return abstain("no_signal", failed=self._failed_rules(sym, bars, mc),
                           cost_bps=str(mc.round_trip_cost_bps))
        if sig.bar_ts != bar_ts or sig.symbol != sym:
            return abstain("signal_bar_mismatch")
        if sig.signal_id in self.planned:
            return abstain("already_planned", signal_id=sig.signal_id)
        if not self.cooldown.allows(sym, sig.bar_ts + self.p.bar_s, sig.breakout_level):
            return abstain("cooldown", signal_id=sig.signal_id)
        payload = {"symbol": sym, "signal_id": sig.signal_id, "bar_ts": sig.bar_ts, "score": sig.score,
                   "entry_ref_price": sig.entry_ref_price, "breakout_level": sig.breakout_level, "atr": sig.atr,
                   "stop_price": sig.stop_price, "reasons": dict(sig.reasons), "market": mc,
                   "strategy_version": sig.strategy_version}
        self.store.append_signal(sig.signal_id, sym, payload, ts=now)
        self._decision("signal", payload, now)
        return sig, False

    def _failed_rules(self, sym: str, bars: Sequence[Candle], mc) -> list[str]:
        if not hasattr(self.engine, "diagnose"):
            return []
        try:
            d = self.engine.diagnose(sym, bars, mc)
            return ["insufficient_data"] if d is None else list(d.failed)
        except Exception:
            return ["diagnose_error"]

    def _enter(self, sig, now: float, rep: TickReport) -> None:
        sym, sid = sig.symbol, sig.signal_id
        rules = self.risk.rules[sym]

        def skip(reason: str, **extra: Any) -> None:
            self._decision("entry_skipped", {"symbol": sym, "signal_id": sid, "reason": reason, **extra}, now)

        block = self._entry_block(rep)
        if block:
            return skip(block)
        if self._symbol_busy(sym):
            return skip("open_order_or_position")
        q = self.feed.quote(sym)
        if not self._fresh(q, now):
            return skip("stale_feed")
        age = (int(now) - (sig.bar_ts + self.p.bar_s)) // self.p.bar_s
        if age >= self.p.max_signal_age_bars:
            return skip("invalidated_signal_age", age_bars=age)
        ceiling = sig.entry_ref_price + self.p.invalidation_atr_mult * sig.atr
        if q.ask > ceiling:
            return skip("invalidated_price", ask=q.ask, ceiling=ceiling)
        limit = round_down(q.ask * (1 + self.p.entry_tolerance_bps / _BPS), rules.price_precision)
        if limit < q.ask:
            limit = q.ask.quantize(Decimal(1).scaleb(-rules.price_precision), rounding=ROUND_CEILING)
        account = self._account(now, {sym: q})
        d = self.feed.depth(sym)
        depth_notional = notional_within(d.asks, limit, asks=True) if d is not None else None
        sized = self.sizing(account.equity, limit, sig.stop_price, rules, self.cfg.risk, account.cash_free,
                            self.cfg.taker_rate, depth_notional, sig.score,
                            min_depth_multiple=self.cfg.min_depth_multiple,
                            exposure_headroom=self._exposure_headroom(account))
        if not sized.ok:
            return skip("sizing", reasons=list(sized.reasons))
        qty = sized.qty
        if self.advisor is not None:
            verdict = self.advisor.review({
                "symbol": sym, "signal_id": sid, "score": sig.score, "entry_ref_price": sig.entry_ref_price,
                "stop_price": sig.stop_price, "breakout_level": sig.breakout_level, "atr": sig.atr,
                "reasons": dict(sig.reasons), "limit_price": limit, "quantity": qty, "notional": qty * limit,
                "equity": account.equity, "bid": q.bid, "ask": q.ask})
            self._decision("advisor_verdict", {"symbol": sym, "signal_id": sid, **verdict.audit()}, now)
            if verdict.vetoed:
                return skip("advisor_veto")
            if verdict.decision == "allow" and verdict.size_multiplier < 1:
                qty = round_down(qty * verdict.size_multiplier, rules.amount_precision)
                if qty <= 0 or qty * limit < min_notional_for(rules, self.cfg.risk):
                    return skip("advisor_shrink_below_min_notional")
        if not 0 < qty <= sized.qty:  # the advisor can only shrink
            return skip("invalid_quantity")
        intent = OrderIntent(sym, Side.BUY, OrderType.LIMIT, qty, limit, sid)
        plan = EntryPlan(sid, sym, intent.client_id, sig.bar_ts, sig.bar_ts + self.p.bar_s, sig.entry_ref_price,
                         sig.stop_price, sig.breakout_level, sig.atr, sig.score, now)
        self.state.add_plan(plan, now)          # persisted BEFORE the order path: never planned twice
        self.plans[sid] = plan
        self.planned.add(sid)
        res = route_to_oms(self.oms, intent, q, account, now)
        reasons = list(res.decision.reasons) if res.decision is not None else []
        self._decision("entry_submitted", {"symbol": sym, "signal_id": sid, "client_id": intent.client_id,
                                           "quantity": qty, "price": limit, "state": res.state.value,
                                           "sent": res.sent, "risk_reasons": reasons,
                                           "sizing": {"binding": sized.binding, "reasons": list(sized.reasons),
                                                      "risk_budget": sized.risk_budget}}, now)
        if res.state is S.REJECTED:
            self._decision("entry_rejected", {"symbol": sym, "signal_id": sid, "client_id": intent.client_id,
                                              "risk_reasons": reasons}, now)
        rep.actions.append(f"entry:{sym}:{res.state.value}")

    # ================================================================== position management
    def _adopt(self, plan: EntryPlan, pos: Position, now: float, rep: TickReport) -> None:
        tot = self.store.fill_totals(plan.client_id)
        entry = tot.quote_amount / tot.quantity if tot.quantity > 0 else pos.avg_price
        tp = TrackedPosition(symbol=plan.symbol, key=plan.signal_id, origin="signal",
                             entry_bar_ts=plan.entry_bar_ts, entry_price=entry, initial_stop=plan.stop_price,
                             current_stop=plan.stop_price, breakout_level=plan.breakout_level,
                             highest_high_since_entry=entry, atr=plan.atr, realized_at_open=pos.realized_pnl,
                             opened_ts=now)
        self._save(tp, now)
        self._decision("position_opened", {"symbol": plan.symbol, "signal_id": plan.signal_id,
                                           "client_id": plan.client_id, "entry_price": entry,
                                           "quantity": pos.quantity, "stop": plan.stop_price}, now)
        rep.actions.append(f"opened:{plan.symbol}")

    def _adopt_orphan(self, sym: str, pos: Position, now: float, rep: TickReport) -> None:
        """A ledger position without bot state (e.g. state lost, manual trade): protect it with a
        conservative stop (entry - 1.5 ATR); without ATR, entry - 5 %, needs_review and exit-only."""
        rules = self.risk.rules.get(sym)
        entry = pos.avg_price
        atr = simple_atr(self.feed.closed_bars(sym, self.p.orphan_atr_period + 1), self.p.orphan_atr_period)
        if atr is not None and entry - self.p.orphan_stop_atr_mult * atr > 0:
            stop, review, why = entry - self.p.orphan_stop_atr_mult * atr, False, ""
        else:
            stop, review, why = entry * (1 - self.p.orphan_fallback_stop_pct / _HUNDRED), True, "orphan_without_atr"
        if rules is not None:
            stop = round_down(stop, rules.price_precision)
        bar = int(now) // self.p.bar_s * self.p.bar_s
        tp = TrackedPosition(symbol=sym, key=f"orph_{sym}_{int(now)}", origin="orphan", entry_bar_ts=bar,
                             entry_price=entry, initial_stop=stop, current_stop=stop, breakout_level=stop,
                             highest_high_since_entry=entry, needs_review=review, review_reason=why, atr=atr,
                             realized_at_open=pos.realized_pnl, opened_ts=now)
        self._save(tp, now)
        self._decision("orphan_adopted", {"symbol": sym, "key": tp.key, "quantity": pos.quantity,
                                          "entry_price": entry, "stop": stop, "atr": atr,
                                          "needs_review": review, "reason": why}, now)
        rep.actions.append(f"orphan:{sym}")

    def _manage(self, sym: str, bars: Sequence[Candle], now: float, rep: TickReport) -> None:
        """Fold newly closed bars into the state, then one exit-policy decision for the newest bar."""
        tp = self.positions[sym]
        new_bars = [b for b in bars if b.ts >= tp.entry_bar_ts and (tp.last_bar_ts is None or b.ts > tp.last_bar_ts)]
        if not new_bars:
            return
        tp = replace(tp, highest_high_since_entry=max([tp.highest_high_since_entry, *(b.high for b in new_bars)]),
                     bars_held=max(0, (bars[-1].ts - tp.entry_bar_ts) // self.p.bar_s + 1),
                     last_bar_ts=bars[-1].ts)
        if tp.origin == "orphan" and tp.atr is None:
            atr = simple_atr(bars, self.p.orphan_atr_period)
            if atr is not None:
                cand = tp.entry_price - self.p.orphan_stop_atr_mult * atr
                tp = replace(tp, atr=atr, current_stop=max(tp.current_stop, cand))
        qty = self.oms.positions().get(sym, Position(sym)).quantity
        action, reason = "hold", ""
        if tp.exit_request is not None and tp.exit_request.target_qty == 0:
            action, reason = "already_exiting", tp.exit_request.reason
        elif any(b.low <= tp.current_stop for b in new_bars):
            action, reason = "exit_all", "stop_touched_on_bar"
            tp = self._request(tp, _ZERO, reason, now, stop_hit=True)
        elif tp.needs_review:
            action, reason = "exit_only", tp.review_reason
        else:
            try:
                d = self.exit_policy.on_bar(tp.contract_state(qty), bars)
            except Exception as e:
                d = None
                self._error("exit_policy", repr(e), now)
            if d is not None:
                action, reason = d.action, d.reason
                if d.action == "exit_all":
                    tp = self._request(tp, _ZERO, d.reason or "exit_all", now)
                elif d.action == "exit_partial" and not tp.partial_taken and 0 < d.fraction < 1:
                    tp = self._request(tp, qty - self._step_down(sym, qty * d.fraction), d.reason or "partial", now)
                elif d.action == "update_stop" and d.new_stop is not None and d.new_stop > tp.current_stop:
                    tp = replace(tp, current_stop=d.new_stop)
        self._save(tp, now)
        self._decision("exit_decision", {"symbol": sym, "signal_id": tp.key, "bar_ts": bars[-1].ts,
                                         "action": action, "reason": reason, "current_stop": tp.current_stop,
                                         "highest_high": tp.highest_high_since_entry, "bars_held": tp.bars_held,
                                         "quantity": qty}, now)

    def _request(self, tp: TrackedPosition, target: Decimal, reason: str, now: float, *, stop_hit: bool = False,
                 bid: Decimal | None = None) -> TrackedPosition:
        """Merge an exit request: the deeper target wins; an existing equal/deeper request is kept."""
        target = max(target, _ZERO)
        cur = tp.exit_request
        if cur is not None and cur.target_qty <= target:
            return tp
        tp = replace(tp, exit_request=ExitRequest(reason, target, now, stop_hit or (cur is not None and cur.stop_hit)))
        self._decision("exit_requested", {"symbol": tp.symbol, "signal_id": tp.key, "reason": reason,
                                          "target_qty": target, "current_stop": tp.current_stop, "bid": bid}, now)
        return tp

    def _place_exit(self, tp: TrackedPosition, now: float, rep: TickReport) -> None:
        sym, req = tp.symbol, tp.exit_request
        rules = self.risk.rules.get(sym)
        if rules is None:
            return self._flag(tp, "no_symbol_rules", now)
        recs = [r for r in self.store.intents_in_states(_ALL_STATES) if r.intent.symbol == sym]
        if any(r.intent.side is Side.SELL and r.state not in TERMINAL_STATES for r in recs):
            return  # never stack exits: wait for the in-flight one (stale LIMITs are canceled)
        ledger_qty = self.oms.positions().get(sym, Position(sym)).quantity
        want = round_down(ledger_qty - req.target_qty, rules.amount_precision)
        if want <= 0 or (req.target_qty > 0 and self._below_min(sym, want)):
            if req.target_qty > 0:  # partial request satisfied; a full exit closes in _sync
                self._save(replace(tp, exit_request=None, partial_taken=True), now)
                self._decision("exit_done", {"symbol": sym, "signal_id": tp.key, "reason": req.reason}, now)
            return
        if tp.needs_review and tp.review_reason.startswith("exit_"):
            return  # gave up earlier: the guardian / operator owns this position now
        try:
            bal = self.oms.broker.balances()
        except Exception as e:
            return self._error("exit_balances", repr(e), now)
        free = bal[rules.base].free if rules.base in bal else _ZERO
        qty = round_down(min(want, free), rules.amount_precision)   # the exchange wins
        if qty <= 0:
            return self._flag(tp, "no_free_base_at_exchange", now)
        mine = [r for r in recs if r.intent.signal_id.startswith(exit_prefix(tp.key))]
        attempts = [r for r in mine if r.created_at >= req.since]
        zero_fill = [r for r in attempts if self.store.fill_totals(r.intent.client_id).quantity == 0]
        if len(zero_fill) >= self.p.max_exit_failures or len(attempts) >= self.p.max_exit_attempts:
            self._error("exit_alert", f"{sym}: exit attempts exhausted, needs review", now)
            return self._flag(tp, "exit_attempts_exhausted", now)
        q = self.feed.quote(sym)
        price = None
        if attempts or q is None:      # an earlier attempt for this request ended short: escalate
            typ = OrderType.MARKET
        else:
            typ = OrderType.LIMIT
            price = round_down(q.bid * (1 - self.p.exit_limit_tolerance_bps / _BPS), rules.price_precision)
            if price <= 0:
                typ, price = OrderType.MARKET, None
        intent = OrderIntent(sym, Side.SELL, typ, qty, price, exit_signal_id(tp.key, len(mine)), reduce_only=True)
        res = route_to_oms(self.oms, intent, q, self._exit_account(now), now)
        self._decision("exit_submitted", {
            "symbol": sym, "signal_id": tp.key, "client_id": intent.client_id, "reason": req.reason,
            "type": typ.value, "quantity": qty, "price": price, "state": res.state.value, "sent": res.sent,
            "attempt": len(attempts) + 1,
            "risk_reasons": list(res.decision.reasons) if res.decision is not None else []}, now)
        rep.actions.append(f"exit:{sym}:{typ.value}:{res.state.value}")

    def _close(self, tp: TrackedPosition, pos: Position, now: float, rep: TickReport) -> None:
        pnl = pos.realized_pnl - tp.realized_at_open
        req = tp.exit_request
        reason = req.reason if req is not None else "external"
        stop_exit = bool(req is not None and req.stop_hit)
        stopped = stop_exit and pnl <= 0          # a losing stop-out blocks re-entry on the same leg
        trend_intact = not stop_exit and reason not in ("external", "false_breakout") \
            and not reason.startswith("momentum_fail")
        bar = int(now) // self.p.bar_s * self.p.bar_s
        self.cooldown.record_exit(tp.symbol, bar, profitable=pnl > 0, trend_intact=trend_intact,
                                  stopped_out=stopped, breakout_level=tp.breakout_level)
        self.state.save_cooldown(self.cooldown.to_dict(), now)
        self.state.close_position(tp, reason, pnl, now)
        del self.positions[tp.symbol]
        rep.actions.append(f"closed:{tp.symbol}")

    def _check_exchange(self, ledger: Mapping[str, Position], now: float) -> None:
        """Exchange wins: a tracked position the exchange holds less of than the ledger is flagged
        needs_review (exit-only); exits are sized from the exchange's free balance anyway."""
        if not self.positions:
            return
        try:
            bal = self.oms.broker.balances()
        except Exception as e:
            return self._error("balances", repr(e), now)
        for sym, tp in sorted(self.positions.items()):
            rules = self.risk.rules.get(sym)
            if rules is None or tp.needs_review:
                continue
            held = bal[rules.base].total if rules.base in bal else _ZERO
            need = round_down(ledger.get(sym, Position(sym)).quantity, rules.amount_precision)
            if held < need:
                self._flag(tp, "exchange_below_ledger", now, exchange=held, ledger=need)

    # ================================================================== helpers
    def _flag(self, tp: TrackedPosition, why: str, now: float, **extra: Any) -> None:
        if tp.needs_review and tp.review_reason == why:
            return
        self._save(replace(tp, needs_review=True, review_reason=why), now)
        self._decision("needs_review", {"symbol": tp.symbol, "signal_id": tp.key, "reason": why, **extra}, now)

    def _save(self, tp: TrackedPosition, now: float) -> None:
        self.state.save_position(tp, now)   # persisted first; memory follows
        self.positions[tp.symbol] = tp

    def _targets(self, tp: TrackedPosition, qty: Decimal) -> tuple:
        if not hasattr(self.exit_policy, "targets"):
            return ()
        try:
            return tuple(self.exit_policy.targets(tp.contract_state(qty)))
        except Exception:
            return ()

    def _step_down(self, sym: str, qty: Decimal) -> Decimal:
        rules = self.risk.rules.get(sym)
        return qty if rules is None else round_down(qty, rules.amount_precision)

    def _mark(self, sym: str, fallback: Decimal) -> Decimal:
        q = self.feed.quote(sym)
        return q.bid if q is not None and q.bid > 0 else fallback

    def _below_min(self, sym: str, qty: Decimal, fallback: Decimal = _ZERO) -> bool:
        rules = self.risk.rules.get(sym)
        if rules is None:
            return qty <= 0
        return qty * self._mark(sym, fallback) < min_notional_for(rules, self.cfg.risk)

    def _is_dust(self, sym: str, pos: Position) -> bool:
        """Nothing sellable: below one amount step, or worth less than the minimum notional."""
        rules = self.risk.rules.get(sym)
        if rules is None:
            return pos.quantity <= 0
        q = round_down(pos.quantity, rules.amount_precision)
        return q <= 0 or self._below_min(sym, q, pos.avg_price)

    def _sells_in_flight(self, sym: str) -> bool:
        return any(r.intent.symbol == sym and r.intent.side is Side.SELL
                   for r in self.store.intents_in_states(_ALL_STATES - TERMINAL_STATES))

    def _symbol_busy(self, sym: str) -> bool:
        """One open order / position per symbol."""
        if sym in self.positions or any(p.symbol == sym for p in self.plans.values()):
            return True
        if any(r.intent.symbol == sym for r in self.store.intents_in_states(_ALL_STATES - TERMINAL_STATES)):
            return True
        pos = self.oms.positions().get(sym)
        return pos is not None and not self._is_dust(sym, pos)

    def _fresh(self, q: Quote | None, now: float) -> bool:
        return q is not None and 0 <= now - q.ts <= self.cfg.max_data_age_s and q.bid > 0 and q.ask >= q.bid

    def _account(self, now: float, extra: Mapping[str, Quote]) -> AccountState:
        quotes: dict[str, Quote] = {}
        for s in set(self.oms.positions()) | set(extra):
            q = extra.get(s) or self.feed.quote(s)
            if q is not None:
                quotes[s] = q
        return snapshot_account(self.store, self.oms.broker.balances(), self.cfg.quote_currency, quotes, now)

    def _exit_account(self, now: float) -> AccountState:
        """Exits never depend on account data: on any failure a neutral state is used (the risk
        engine's exit path only reads it for an informational breach flag)."""
        try:
            return self._account(now, {})
        except Exception as e:
            self._error("exit_account", repr(e), now)
            return AccountState(_ZERO, _ZERO, _ZERO, utc_day(now), _ZERO)

    def _exposure_headroom(self, account: AccountState) -> Decimal:
        held = sum((p.quantity * account.marks.get(s, p.avg_price) for s, p in self.oms.positions().items()
                    if p.quantity > 0), _ZERO)
        pending = sum((i.quantity * (i.price if i.price is not None else account.marks.get(i.symbol, _ZERO))
                       for i in self.oms.open_intents() if i.side is Side.BUY), _ZERO)
        return account.equity * self.cfg.risk.max_exposure_pct / _HUNDRED - held - pending

    def _decision(self, kind: str, payload: Any, now: float) -> None:
        """Decision-log row; a failure to log is recorded but never stops trading or exits."""
        try:
            self.state.decision(kind, payload, now)
        except Exception as e:
            log.exception("bot: could not record %s", kind)
            self._error("audit", repr(e), now)

    def _error(self, where: str, message: str, now: float) -> None:
        try:
            self.store.append_error(f"bot.{where}", message, ts=now)
        except Exception:
            log.exception("bot: could not record an error")
