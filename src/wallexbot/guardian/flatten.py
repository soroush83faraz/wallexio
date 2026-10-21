"""Hard-breach flatten procedure (docs/RISK.md, "Hard breach and flatten procedure").

Steps, in order:
1. Engage the kill switch (entries off). Reduce-only exits are never blocked by it.
2. Cancel ENTRY orders only (every open BUY at the exchange; the bot is long-only) and verify
   with broker.open_orders(). The OMS ledger is then synced with oms.reconcile().
3. Per position, place reduce-only SELL exits through OMS.submit (the only path to the broker):
   LIMIT at bid minus `limit_tolerance_bps`, escalated to MARKET once a LIMIT of this episode
   timed out or failed; MARKET from the start on a hard breach. Every child order is at most
   `max_depth_fraction` (50%) of the visible bid depth that the order can reach (MARKET: within
   `market_band_pct` of the best bid, because Wallex auto-cancels market orders beyond book depth
   or a 2% move; LIMIT: levels at or above the limit price). Exits are sized from the CONFIRMED
   exchange balance, capped by the OMS ledger (the OMS cannot sell what its ledger does not hold).
4. Poll until flat, or until `max_failed_attempts` failed exits, then record needs_review and an
   operator alert (an `errors` row with source guardian.alert, plus the returned status).

Invariants (why this can never double-sell or run away):
- No resubmission. A new exit is placed only when every reduce-only SELL intent for the symbol
  that could still sell (this episode's own, or anyone's in unknown_pending / risk_approved) is
  terminal. An ambiguous submit stays unknown_pending until oms.reconcile() resolves it; we wait
  for that (bounded), we never send a replacement for it.
- Deterministic identity. signal_id = gflat:<UTC day>-<cause>:<symbol>:<n>, where n is the number
  of intents this episode already holds for the symbol (durable state). With the same durable
  state a re-run builds the same client_id, which the OMS refuses to send twice.
- Sizing from confirmed balances: an order can only ever sell base the exchange shows as free.
- Bounded work. Failed exits are counted from durable state (terminal with zero fill, or
  needs_review) plus in-call failures (unresolved after a bounded wait, read errors); at
  `max_failed_attempts` the symbol is given up. Orders per symbol per episode are capped by
  `max_orders_per_symbol`, loop iterations by a fixed cap, every wait by a deadline AND a poll
  count, so a clock that does not advance cannot hang it either.
- Running flatten twice in a row: the second run sees the first run's intents (all terminal, or
  waited on), a flat balance, or the durable failure count, so it places no duplicate order.

This module must not import wallexbot.strategy, wallexbot.backtest or wallexbot.ai, nor the
broker modules; it receives the broker object (reads, cancels) and the OMS (the only send path).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Sequence

from wallexbot.oms import OPEN_INTENT_STATES
from wallexbot.risk.engine import AccountState, utc_day
from wallexbot.store import IntentRecord
from wallexbot.types import (TERMINAL_STATES, IntentState, OrderIntent, OrderType, Side, SymbolRules,
                             round_down)

S = IntentState
ACTOR = "guardian"
PREFIX = "gflat"
_ZERO = Decimal(0)
_BPS = Decimal(10000)
_HUNDRED = Decimal(100)
_LIVE = frozenset({S.SUBMITTED, S.PARTIALLY_FILLED})
_AMBIGUOUS = frozenset({S.UNKNOWN_PENDING, S.RISK_APPROVED})
_STOP_TYPES = frozenset({OrderType.STOP_LIMIT, OrderType.STOP_MARKET})
log = logging.getLogger("wallexbot.guardian")


@dataclass(frozen=True)
class FlattenConfig:
    limit_tolerance_bps: Decimal = Decimal("20")   # LIMIT exit price = bid * (1 - 20 bps)
    limit_timeout_s: float = 10.0                   # a resting LIMIT exit is canceled after this
    market_timeout_s: float = 5.0                   # a MARKET exit still open after this is canceled
    unknown_timeout_s: float = 60.0                 # wait for unknown_pending to resolve (> OMS grace)
    cancel_timeout_s: float = 10.0                  # wait for a cancel to show up as terminal
    poll_interval_s: float = 1.0
    max_depth_fraction: Decimal = Decimal("0.5")    # each child <= 50% of reachable visible depth
    market_band_pct: Decimal = Decimal("2")         # Wallex auto-cancels market orders beyond 2%
    max_failed_attempts: int = 3
    max_orders_per_symbol: int = 20
    # Wallex stop-order balance locking is unverified. When True, protective stops and any other
    # resting non-guardian SELL for the symbol are canceled immediately before the guardian sells
    # (otherwise their locked base could not be sold, or an unlocked stop could fire afterwards).
    cancel_protective_stops: bool = True
    dry_run: bool = False                           # shadow mode: plan and audit, no broker writes
    dust_notional: Decimal | None = None            # None: rules.min_notional or risk.min_notional

    def __post_init__(self) -> None:
        """Bounded work depends on these: refuse configs that would make a wait unbounded."""
        for name, v in (("limit_timeout_s", self.limit_timeout_s), ("market_timeout_s", self.market_timeout_s),
                        ("unknown_timeout_s", self.unknown_timeout_s), ("cancel_timeout_s", self.cancel_timeout_s)):
            if not 0 < v <= 3600:
                raise ValueError(f"{name} must be in (0, 3600] seconds")
        if not 0.01 <= self.poll_interval_s <= 60:
            raise ValueError("poll_interval_s must be in [0.01, 60] seconds")
        if not 0 < self.max_depth_fraction <= 1 or not 0 < self.market_band_pct < 100:
            raise ValueError("max_depth_fraction must be in (0, 1], market_band_pct in (0, 100)")
        if not 1 <= self.max_failed_attempts <= 10 or not 1 <= self.max_orders_per_symbol <= 200:
            raise ValueError("max_failed_attempts in [1, 10], max_orders_per_symbol in [1, 200]")
        if not 0 <= self.limit_tolerance_bps < 10000:
            raise ValueError("limit_tolerance_bps must be in [0, 10000)")


@dataclass
class SymbolOutcome:
    symbol: str
    status: str = "pending"   # flat | dust | needs_review | resting_exits | dry_run | pending
    reason: str = ""
    orders: list[str] = field(default_factory=list)   # client_ids handed to OMS.submit this call
    failures: int = 0
    remaining: Decimal = _ZERO                         # confirmed exchange base total at the end
    notes: list[str] = field(default_factory=list)


@dataclass
class FlattenResult:
    episode: str
    flat: bool
    needs_review: bool
    alerts: list[str] = field(default_factory=list)
    canceled_entries: list[str] = field(default_factory=list)
    symbols: dict[str, SymbolOutcome] = field(default_factory=dict)


def episode_id(now: float, cause: str) -> str:
    return f"{utc_day(now)}-{cause}"


def is_guardian_intent(rec: IntentRecord) -> bool:
    return rec.intent.signal_id.startswith(PREFIX + ":")


def recover_guardian_intents(store, now: float) -> list[str]:
    """Startup only, single guardian instance: settle the guardian's OWN intents interrupted
    mid-submit by a previous guardian process (PROPOSED never reached the broker -> rejected;
    RISK_APPROVED may have -> unknown_pending, which reconcile resolves). Never touches others'."""
    done = []
    for rec in store.intents_in_states({S.PROPOSED, S.RISK_APPROVED}):
        if not is_guardian_intent(rec):
            continue
        new = S.REJECTED if rec.state is S.PROPOSED else S.UNKNOWN_PENDING
        try:
            store.set_intent_state(rec.intent.client_id, new, expected=rec.state,
                                   reason="guardian_recover", ts=now)
            done.append(rec.intent.client_id)
        except Exception as e:
            store.append_error("guardian.recover", repr(e), {"client_id": rec.intent.client_id}, ts=now)
    return done


def flatten_all(store, broker, oms, killswitch, *, cause: str, hard: bool,
                config: FlattenConfig = FlattenConfig(), clock: Callable[[], float] = time.time,
                sleep: Callable[[float], Any] = time.sleep, account: AccountState | None = None,
                reason: str = "", depth_fn: Callable[[str], Sequence] | None = None) -> FlattenResult:
    """Run one flatten episode pass. `cause` names the episode (e.g. "hard", "stale", "operator")."""
    return _Flattener(store, broker, oms, killswitch, cause=cause, hard=hard, config=config, clock=clock,
                      sleep=sleep, account=account, reason=reason, depth_fn=depth_fn).run()


class _Flattener:
    def __init__(self, store, broker, oms, killswitch, *, cause, hard, config, clock, sleep, account,
                 reason, depth_fn) -> None:
        self.store, self.broker, self.oms, self.ks = store, broker, oms, killswitch
        self.cfg: FlattenConfig = config
        self.clock, self._sleep_fn = clock, sleep
        self.hard, self.reason = bool(hard), reason
        self.rules: dict[str, SymbolRules] = dict(oms.risk.rules)
        now = float(clock())
        self.episode = episode_id(now, cause)
        self.account = account or AccountState(_ZERO, _ZERO, _ZERO, utc_day(now), _ZERO)
        self.depth_fn = depth_fn or (lambda sym: broker.depth(sym).bids)
        self.alerts: list[str] = []
        # Unresolved ambiguous submits must be allowed to reach the OMS not-found grace period.
        grace = float(oms.not_found_grace_s)
        self.unknown_timeout = max(config.unknown_timeout_s, grace + 2 * config.poll_interval_s)

    # ------------------------------------------------------------------ top level
    def run(self) -> FlattenResult:
        now = self._now()
        self._audit("flatten_start", {"episode": self.episode, "hard": self.hard, "reason": self.reason,
                                      "dry_run": self.cfg.dry_run})
        self._engage(now)
        canceled, entries_ok = self._cancel_entries()
        self._reconcile()
        res = FlattenResult(self.episode, flat=False, needs_review=False, canceled_entries=canceled)
        for sym in self._symbols():
            try:
                res.symbols[sym] = self._flatten_symbol(sym)
            except Exception as e:  # one symbol's failure must not stop the others
                out = SymbolOutcome(sym, notes=[f"error:{type(e).__name__}"])
                self._error("guardian.flatten", f"{sym}: {e!r}")
                self._give_up(out, f"exception:{type(e).__name__}")
                res.symbols[sym] = out
        if not entries_ok:
            self._alert("entry_cancel_unverified", None)
        res.alerts = list(self.alerts)
        res.needs_review = (not entries_ok) or any(o.status == "needs_review" for o in res.symbols.values())
        res.flat = entries_ok and not self.cfg.dry_run and \
            all(o.status in ("flat", "dust") for o in res.symbols.values())
        self._audit("flatten_done", {"episode": self.episode, "flat": res.flat, "needs_review": res.needs_review,
                                     "symbols": {s: {"status": o.status, "reason": o.reason, "orders": o.orders,
                                                     "remaining": o.remaining}
                                                 for s, o in res.symbols.items()}})
        return res

    # ------------------------------------------------------------------ steps 1 and 2
    def _engage(self, now: float) -> None:
        if self.ks.is_engaged():
            return
        self.ks.engage(f"guardian:flatten:{self.episode}:{self.reason}", actor=ACTOR, ts=now)
        self._audit("engage_kill_switch", {"episode": self.episode, "why": "flatten"})

    def _cancel_entries(self) -> tuple[list[str], bool]:
        canceled: list[str] = []
        for _round in range(2):
            try:
                buys = [o for o in self.broker.open_orders() if o.side is Side.BUY]
            except Exception as e:
                self._error("guardian.flatten", f"open_orders failed: {e!r}")
                return canceled, False
            if not buys:
                return canceled, True
            if self.cfg.dry_run:
                self._audit("plan_cancel_entries", {"client_ids": [o.client_id for o in buys]})
                return canceled, True
            for o in buys:
                self._cancel(o.client_id, "cancel_entry")
                canceled.append(o.client_id)
            self._reconcile()
        try:  # verify after the second round
            still = [o.client_id for o in self.broker.open_orders() if o.side is Side.BUY]
        except Exception as e:
            self._error("guardian.flatten", f"open_orders verify failed: {e!r}")
            return canceled, False
        return canceled, not still

    def _symbols(self) -> list[str]:
        held = {s for s, p in self.oms.positions().items() if p.quantity > 0 and s in self.rules}
        try:
            bal = self.broker.balances()
            held |= {s for s, r in self.rules.items() if self._total(bal, r.base) > 0}
        except Exception as e:
            self._error("guardian.flatten", f"balances failed: {e!r}")
        return sorted(held)

    # ------------------------------------------------------------------ steps 3 and 4
    def _flatten_symbol(self, sym: str) -> SymbolOutcome:
        out = SymbolOutcome(sym)
        if self.cfg.dry_run:
            return self._plan_only(sym, out)
        foreign_done = False
        local_fail = 0  # failures not visible in durable state (unresolved waits, read errors)
        max_iter = self.cfg.max_orders_per_symbol + 3 * self.cfg.max_failed_attempts + 5
        for _ in range(max_iter):
            self._reconcile()
            mine = self._episode_intents(sym)
            out.failures = sum(1 for r in mine if self._failed(r)) + local_fail
            if out.failures >= self.cfg.max_failed_attempts:
                return self._give_up(out, "max_failed_attempts")
            if len(mine) >= self.cfg.max_orders_per_symbol:
                return self._give_up(out, "max_orders_per_symbol")
            blocking = self._blocking_exits(sym)
            if blocking:
                if not all([self._settle(r) for r in blocking]):
                    local_fail += 1
                    out.notes.append("unresolved_exit")
                continue
            if self.cfg.cancel_protective_stops and not foreign_done:
                foreign_done = True
                if self._cancel_foreign_exits(sym):
                    continue
            snap = self._snapshot(sym)
            if snap is None:
                local_fail += 1
                out.notes.append("snapshot_failed")
                self._sleep()
                continue
            done = self._final_status(sym, snap, out)
            if done is not None:
                return done
            if not self._place_exit(sym, snap, len(mine), self._escalate(mine), out):
                local_fail += 1
                out.notes.append("exit_not_placed")
                self._sleep()
        return self._give_up(out, "iteration_cap")

    # ------------------------------------------------------------------ state reads
    def _episode_intents(self, sym: str) -> list[IntentRecord]:
        prefix = f"{PREFIX}:{self.episode}:{sym}:"
        return [r for r in self.store.intents_in_states(set(IntentState)) if r.intent.signal_id.startswith(prefix)]

    def _failed(self, rec: IntentRecord) -> bool:
        if rec.state is S.NEEDS_REVIEW:
            return True
        return rec.state in (S.REJECTED, S.CANCELED) and \
            self.store.fill_totals(rec.intent.client_id).quantity == 0

    def _escalate(self, mine: list[IntentRecord]) -> bool:
        return self.hard or any(r.intent.type is OrderType.LIMIT and r.state in (S.CANCELED, S.REJECTED,
                                                                                  S.NEEDS_REVIEW)
                                for r in mine)

    def _blocking_exits(self, sym: str) -> list[IntentRecord]:
        """Reduce-only SELLs that could still sell base we are about to size: ours while open,
        anyone's while ambiguous. Others' confirmed resting exits hold locked base, which the
        confirmed free balance already excludes."""
        return [r for r in self.store.intents_in_states(OPEN_INTENT_STATES)
                if r.intent.symbol == sym and r.intent.side is Side.SELL
                and (is_guardian_intent(r) or r.state in _AMBIGUOUS)]

    def _snapshot(self, sym: str) -> dict[str, Decimal] | None:
        r = self.rules[sym]
        try:
            bal = self.broker.balances()
        except Exception as e:
            self._error("guardian.flatten", f"balances failed: {e!r}")
            return None
        ledger = self.oms.positions().get(sym)
        ledger_qty = ledger.quantity if ledger is not None else _ZERO
        committed = sum((i.quantity for i in self.oms.open_intents()
                         if i.symbol == sym and i.side is Side.SELL), _ZERO)
        try:
            mark = self.broker.quote(sym).bid
        except Exception:
            mark = ledger.avg_price if ledger is not None else _ZERO
        free = bal[r.base].free if r.base in bal else _ZERO
        total = self._total(bal, r.base)
        return {"free": free, "total": total, "ledger": ledger_qty, "committed": committed, "mark": mark,
                "sellable": round_down(max(min(free, ledger_qty - committed), _ZERO), r.amount_precision)}

    def _dust(self, sym: str, qty: Decimal, mark: Decimal) -> bool:
        r = self.rules[sym]
        if round_down(qty, r.amount_precision) <= 0:
            return True
        return mark > 0 and qty * mark < self._min_notional(sym)

    def _min_notional(self, sym: str) -> Decimal:
        if self.cfg.dust_notional is not None:
            return self.cfg.dust_notional
        r = self.rules[sym]
        if r.min_notional is not None:
            return r.min_notional
        try:
            return self.oms.risk.config.risk.min_notional
        except Exception:
            return _ZERO

    def _final_status(self, sym: str, snap: dict[str, Decimal], out: SymbolOutcome) -> SymbolOutcome | None:
        out.remaining = snap["total"]
        exch_dust = self._dust(sym, snap["total"], snap["mark"])
        ledger_dust = self._dust(sym, snap["ledger"], snap["mark"])
        if exch_dust and ledger_dust:
            out.status = "flat" if snap["total"] == 0 and snap["ledger"] == 0 else "dust"
            return out
        if exch_dust:  # exchange wins: the ledger holds base the exchange does not show
            return self._give_up(out, "ledger_without_balance")
        if ledger_dust:  # the exchange holds base the OMS ledger does not know: cannot sell via OMS
            return self._give_up(out, "untracked_exchange_balance")
        if self._dust(sym, snap["sellable"], snap["mark"]):
            if snap["total"] > snap["free"] and not self.cfg.cancel_protective_stops:
                out.status, out.reason = "resting_exits", "base locked by resting non-guardian exits"
                return out
            return self._give_up(out, "balance_not_sellable")
        return None

    # ------------------------------------------------------------------ order placement
    def _place_exit(self, sym: str, snap: dict[str, Decimal], n: int, escalate: bool,
                    out: SymbolOutcome) -> bool:
        r = self.rules[sym]
        try:
            quote = self.broker.quote(sym)
        except Exception as e:
            self._error("guardian.flatten", f"{sym} quote failed: {e!r}")
            return False
        typ, price, floor, why = self._order_shape(r, quote.bid, escalate)
        try:
            levels = list(self.depth_fn(sym))
        except Exception:
            levels = None
        if levels is None:
            # Unknown depth: never a MARKET of unknown impact; a LIMIT bounds the worst price.
            typ, price, floor, why = self._order_shape(r, quote.bid, False)
            qty = snap["sellable"]
            why += ",depth_unavailable"
        else:
            visible = sum((q for p, q in levels if p >= floor), _ZERO)
            qty = min(snap["sellable"], round_down(visible * self.cfg.max_depth_fraction, r.amount_precision))
        if qty <= 0:
            out.notes.append("no_visible_depth")
            return False
        intent = OrderIntent(sym, Side.SELL, typ, qty, price, f"{PREFIX}:{self.episode}:{sym}:{n}",
                             reduce_only=True)
        existed = self.store.get_intent(intent.client_id) is not None
        res = self.oms.submit(intent, quote, self.account, now=self._now())
        out.orders.append(intent.client_id)
        self._audit("flatten_exit", {"episode": self.episode, "symbol": sym, "client_id": intent.client_id,
                                     "type": typ.value, "quantity": qty, "price": price, "why": why,
                                     "sent": res.sent, "state": res.state.value})
        return not existed

    def _order_shape(self, r: SymbolRules, bid: Decimal, escalate: bool):
        if escalate:
            floor = bid * (1 - self.cfg.market_band_pct / _HUNDRED)
            return OrderType.MARKET, None, floor, "hard_breach" if self.hard else "escalated"
        price = round_down(bid * (1 - self.cfg.limit_tolerance_bps / _BPS), r.price_precision)
        return OrderType.LIMIT, price, price, "limit_first"

    # ------------------------------------------------------------------ settling exits
    def _settle(self, rec: IntentRecord) -> bool:
        """Bring one blocking exit to a terminal state within bounded time. True iff terminal."""
        cid = rec.intent.client_id
        if rec.state in _AMBIGUOUS:
            rec = self._wait(cid, self._now() + self.unknown_timeout, lambda st: st not in _AMBIGUOUS)
        if rec.state in _LIVE and is_guardian_intent(rec):
            timeout = self.cfg.limit_timeout_s if rec.intent.type is OrderType.LIMIT else self.cfg.market_timeout_s
            rec = self._wait(cid, rec.created_at + timeout, lambda st: st in TERMINAL_STATES)
            if rec.state in _LIVE:  # confirmed live at the exchange and timed out: cancel, never resend
                self._cancel(cid, "exit_timeout_cancel")
                rec = self._wait(cid, self._now() + self.cfg.cancel_timeout_s, lambda st: st in TERMINAL_STATES)
        if rec.state in TERMINAL_STATES:
            return True
        # Someone else's exit that turned out to be a confirmed resting order no longer blocks:
        # its base is locked at the exchange, so the confirmed free balance excludes it.
        return not is_guardian_intent(rec) and rec.state not in _AMBIGUOUS

    def _wait(self, cid: str, deadline: float, done: Callable[[IntentState], bool]) -> IntentRecord:
        start = self._now()
        max_polls = int(max(deadline - start, 0) / max(self.cfg.poll_interval_s, 1e-6)) + 2
        rec = self.store.get_intent(cid)
        for _ in range(max_polls):
            if done(rec.state) or self._now() >= deadline:
                return rec
            self._sleep()
            self._reconcile()
            rec = self.store.get_intent(cid)
        return rec

    def _cancel_foreign_exits(self, sym: str) -> bool:
        try:
            sells = [o for o in self.broker.open_orders(sym) if o.side is Side.SELL]
        except Exception as e:
            self._error("guardian.flatten", f"open_orders({sym}) failed: {e!r}")
            return False
        foreign = []
        for o in sells:
            rec = self.store.get_intent(o.client_id)
            if rec is None or not is_guardian_intent(rec):
                foreign.append(o)
        for o in foreign:
            kind = "cancel_protective_stop" if (o.type in _STOP_TYPES or o.stop_price is not None) \
                else "cancel_resting_exit"
            self._cancel(o.client_id, kind)
        if foreign:
            self._reconcile()
        return bool(foreign)

    def _cancel(self, cid: str, kind: str) -> None:
        try:
            order = self.broker.cancel_order(cid)
            status = None if order is None else order.status.value
        except Exception as e:
            self._error("guardian.flatten", f"cancel {kind} failed: {e!r}", {"client_id": cid})
            status = f"error:{type(e).__name__}"
        self._audit(kind, {"episode": self.episode, "client_id": cid, "result": status})

    # ------------------------------------------------------------------ dry run
    def _plan_only(self, sym: str, out: SymbolOutcome) -> SymbolOutcome:
        snap = self._snapshot(sym)
        if snap is None:
            out.status, out.reason = "dry_run", "snapshot_failed"
            return out
        out.remaining = snap["total"]
        out.status, out.reason = "dry_run", f"would sell {snap['sellable']}"
        self._audit("plan_exit", {"episode": self.episode, "symbol": sym, "sellable": snap["sellable"],
                                  "escalate": self.hard})
        return out

    # ------------------------------------------------------------------ give up / alert
    def _give_up(self, out: SymbolOutcome, reason: str) -> SymbolOutcome:
        out.status, out.reason = "needs_review", reason
        self._alert(reason, out.symbol)
        return out

    def _alert(self, reason: str, sym: str | None) -> None:
        key = {"episode": self.episode, "symbol": sym, "reason": reason}
        self.alerts.append(f"{sym or '*'}:{reason}")
        try:
            seen = any({k: e["payload"].get(k) for k in key} == key
                       for e in self.store.audit_events(ACTOR, "alert"))
        except Exception:
            seen = False
        if seen:  # one alert per episode/symbol/reason, however often the guardian re-runs
            return
        self._audit("needs_review", key)
        self._audit("alert", key)
        self._error("guardian.alert", f"flatten needs operator review: {sym or '*'} {reason}", key)
        log.error("guardian alert: flatten needs operator review: %s %s (%s)", sym, reason, self.episode)

    # ------------------------------------------------------------------ plumbing
    def _now(self) -> float:
        return float(self.clock())

    def _sleep(self) -> None:
        self._sleep_fn(self.cfg.poll_interval_s)

    def _reconcile(self) -> None:
        try:
            self.oms.reconcile(now=self._now())
        except Exception as e:
            self._error("guardian.flatten", f"reconcile failed: {e!r}")

    @staticmethod
    def _total(bal, asset: str) -> Decimal:
        b = bal.get(asset)
        return _ZERO if b is None else b.free + b.locked

    def _audit(self, kind: str, payload: Any) -> None:
        try:
            self.store.append_audit_event(ACTOR, kind, payload, ts=self._now())
        except Exception as e:
            log.error("guardian audit write failed: %r", e)

    def _error(self, source: str, message: str, payload: Any = None) -> None:
        try:
            self.store.append_error(source, message, payload, ts=self._now())
        except Exception as e:
            log.error("guardian error write failed: %r (%s)", e, message)
