"""Guardian: the independent emergency layer (docs/RISK.md).

It runs in its own process with its own Store connection and reads the EXCHANGE as the source of
truth (balances, open orders, quotes). It imports nothing from wallexbot.strategy,
wallexbot.backtest or wallexbot.ai (enforced by tests/test_guardian_isolation.py), and it never
touches the broker's order entry: exits go through OMS.submit inside flatten.py.

One tick:
1. oms.reconcile() so the ledger reflects every known order (ambiguous ones included).
2. Read balances, open orders and a quote per symbol of the risk universe from the broker.
3. Equity = quote-currency balance (free + locked) + base holdings (free + locked) x bid.
   Start-of-day equity is the first snapshot per UTC day (store.append_equity_sod: first write
   wins), the high-water mark is appended when exceeded. Both are written only from fresh,
   fully marked data, so a stale feed cannot set them.
4. Conditions:
   - soft breach (RiskEngine.breaches)                       -> kill switch (entries off)
   - hard breach                                             -> flatten_all(hard=True)
   - exchange base balance != OMS ledger beyond tolerance,
     on `mismatch_confirm_ticks` consecutive ticks            -> kill switch + needs_review
     (exchange wins: nothing in the ledger is "fixed"; flatten sizes from exchange balances)
   - no fresh quote for > max_data_age_s                     -> kill switch (freeze entries)
   - no fresh quote for > stale_action_after_s (120 s)       -> per config: hold, or flatten
   - strategy heartbeat older than heartbeat_timeout_s       -> kill switch
   - breach evaluation error                                 -> kill switch (fail closed)
   The guardian only ever ENGAGES the kill switch; clearing it is an operator action.
5. Every decision is appended to audit_events with actor='guardian' (and kill-switch rows carry
   actor='guardian').
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Sequence

from wallexbot.guardian.flatten import ACTOR, FlattenConfig, FlattenResult, flatten_all
from wallexbot.risk.engine import AccountState, utc_day
from wallexbot.types import IntentState, Side

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)
_IN_FLIGHT = frozenset({IntentState.PROPOSED, IntentState.RISK_APPROVED})
log = logging.getLogger("wallexbot.guardian")


@dataclass(frozen=True)
class GuardianConfig:
    quote_currency: str | None = None          # None: risk.config.quote_currency
    max_data_age_s: float | None = None         # None: risk.config.max_data_age_s (30 s)
    stale_action_after_s: float = 120.0
    stale_action: str = "hold"                  # "hold" | "flatten"
    heartbeat_component: str = "strategy"
    heartbeat_timeout_s: float = 60.0
    # Balance mismatch tolerance per base asset: max(abs_units x smallest amount step,
    # rel_pct % of the ledger quantity). Fees are booked exactly by the OMS, so any real
    # difference is a missed fill, a manual trade or a ledger fault.
    mismatch_abs_units: int = 1
    mismatch_rel_pct: Decimal = Decimal("0.1")
    mismatch_confirm_ticks: int = 2             # transient races (fill seen before ack) self-clear
    in_flight_grace_s: float = 30.0             # a mid-submit intent younger than this skips the check
    flatten: FlattenConfig = field(default_factory=FlattenConfig)

    def __post_init__(self) -> None:
        if self.stale_action not in ("hold", "flatten"):
            raise ValueError(f"stale_action must be 'hold' or 'flatten', not {self.stale_action!r}")


@dataclass
class TickReport:
    ts: float
    equity: Decimal | None = None
    conditions: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    flatten: FlattenResult | None = None


class Guardian:
    def __init__(self, store, broker, risk, killswitch, oms, config: GuardianConfig | None = None,
                 clock: Callable[[], float] = time.time, *, sleep: Callable[[float], Any] = time.sleep,
                 depth_fn: Callable[[str], Sequence] | None = None) -> None:
        self.store, self.broker, self.risk, self.ks, self.oms = store, broker, risk, killswitch, oms
        self.cfg = config or GuardianConfig()
        self.clock, self.sleep, self.depth_fn = clock, sleep, depth_fn
        self.quote_ccy = self.cfg.quote_currency or risk.config.quote_currency
        self.max_age = float(self.cfg.max_data_age_s if self.cfg.max_data_age_s is not None
                             else risk.config.max_data_age_s)
        self.started_at = float(clock())
        self._last_fresh: dict[str, float] = {}
        self._mismatch_streak: dict[str, int] = {}
        self._reviewed: frozenset[str] = frozenset()
        self._last_logged: tuple | None = None

    # ------------------------------------------------------------------ public
    def tick(self) -> TickReport:
        now = float(self.clock())
        rep = TickReport(now)
        self._reconcile(now)
        balances = self.broker.balances()          # a failed read fails the tick (the loop counts it)
        try:
            open_orders = self.broker.open_orders()
        except Exception as e:  # breach handling must not depend on this read
            self._error("guardian.monitor", f"open_orders failed: {e!r}", now)
            rep.conditions.append("open_orders_unavailable")
            open_orders = []
        positions = self.oms.positions()
        quotes = self._quotes(now)
        rules = self.risk.rules

        held = sorted(s for s, r in rules.items()
                      if self._total(balances, r.base) > 0 or self._qty(positions, s) > 0)
        watch = held or sorted(rules)
        feed_age = max((now - self._last_fresh.get(s, self.started_at) for s in watch), default=0.0)
        if feed_age > self.max_age:
            rep.conditions.append("stale_feed")

        equity, unmarked = self._equity(balances, quotes, positions)
        rep.equity = equity
        day = utc_day(now)
        if "stale_feed" not in rep.conditions and not unmarked:
            self.store.append_equity_sod(day, equity, ts=now)
            hwm = self.store.equity_hwm()
            if hwm is None or equity > hwm:
                self.store.append_equity_hwm(equity, ts=now)
        elif unmarked:
            rep.conditions.append("unmarked:" + ",".join(unmarked))
        sod, hwm = self.store.sod_equity(day), self.store.equity_hwm()
        hard: tuple[str, ...] = ()
        cash = balances.get(self.quote_ccy)
        if sod is not None and hwm is not None:
            account = AccountState(equity=equity, cash_free=cash.free if cash else _ZERO, sod_equity=sod,
                                   sod_day=day, hwm_equity=hwm,
                                   marks={s: q.bid for s, q in quotes.items()})
            try:
                b = self.risk.breaches(account)
                rep.conditions += list(b.soft) + list(b.hard)
                hard = tuple(b.hard)
            except Exception as e:
                rep.conditions.append(f"breach_eval_error:{type(e).__name__}")
        else:
            account = None
            rep.conditions.append("no_reference_equity")

        mismatched = self._mismatches(balances, positions, now)
        if mismatched:
            rep.conditions.append("balance_mismatch:" + ",".join(sorted(mismatched)))
            new = frozenset(mismatched) - self._reviewed
            if new:
                self._needs_review("balance_mismatch", {a: mismatched[a] for a in sorted(new)}, now)
                rep.actions.append("needs_review")
        self._reviewed = frozenset(mismatched)

        hb = self.store.latest_event_ts(self.cfg.heartbeat_component, "heartbeat")
        if now - (hb if hb is not None else self.started_at) > self.cfg.heartbeat_timeout_s:
            rep.conditions.append("heartbeat_lost")

        freeze = [c for c in rep.conditions
                  if c.split(":")[0] in ("stale_feed", "heartbeat_lost", "balance_mismatch", "breach_eval_error",
                                         "daily_loss_soft", "drawdown_soft", "daily_loss_hard", "drawdown_hard")]
        if freeze and not self.ks.is_engaged():
            self.ks.engage("guardian: " + ",".join(freeze), actor=ACTOR, ts=now)
            rep.actions.append("engage_kill_switch")
            self._audit("engage_kill_switch", {"reasons": freeze}, now)

        exposure = bool(held) or any(o.side is Side.BUY for o in open_orders)
        if hard and exposure:
            rep.flatten = self._flatten("hard", True, ",".join(hard), account)
            rep.actions.append("flatten:hard")
        elif feed_age > self.cfg.stale_action_after_s and held:
            if self.cfg.stale_action == "flatten":
                rep.flatten = self._flatten("stale", False, f"feed_age={int(feed_age)}s", account)
                rep.actions.append("flatten:stale")
            else:
                rep.conditions.append("stale_hold")

        key = (tuple(rep.conditions), tuple(rep.actions))
        if rep.actions or key != self._last_logged:
            self._audit("tick", {"equity": equity, "sod": sod, "hwm": hwm, "feed_age_s": round(feed_age, 3),
                                 "conditions": rep.conditions, "actions": rep.actions,
                                 "flat": None if rep.flatten is None else rep.flatten.flat,
                                 "needs_review": None if rep.flatten is None else rep.flatten.needs_review}, now)
            self._last_logged = key
        return rep

    # ------------------------------------------------------------------ pieces
    def _flatten(self, cause: str, hard: bool, reason: str, account: AccountState | None) -> FlattenResult:
        return flatten_all(self.store, self.broker, self.oms, self.ks, cause=cause, hard=hard,
                           config=self.cfg.flatten, clock=self.clock, sleep=self.sleep, account=account,
                           reason=reason, depth_fn=self.depth_fn)

    def _quotes(self, now: float) -> dict[str, Any]:
        out = {}
        for sym in self.risk.rules:
            try:
                q = self.broker.quote(sym)
            except Exception as e:
                self._error("guardian.monitor", f"quote {sym} failed: {e!r}", now)
                continue
            out[sym] = q
            if q.bid > 0 and q.ask >= q.bid:
                self._last_fresh[sym] = max(self._last_fresh.get(sym, float("-inf")), float(q.ts))
        return out

    def _equity(self, balances, quotes, positions) -> tuple[Decimal, list[str]]:
        cash = balances.get(self.quote_ccy)
        equity = (cash.free + cash.locked) if cash is not None else _ZERO
        unmarked: list[str] = []
        marked_assets: set[str] = set()
        for sym, r in self.risk.rules.items():
            if r.quote != self.quote_ccy or r.base in marked_assets:
                continue
            qty = self._total(balances, r.base)
            if qty == 0:
                continue
            q = quotes.get(sym)
            if q is None:
                pos = positions.get(sym)
                if pos is None or pos.avg_price <= 0:
                    unmarked.append(r.base)
                    continue
                mark = pos.avg_price  # degraded; SOD/HWM are not written from it
                unmarked.append(r.base)
            else:
                mark = q.bid
            marked_assets.add(r.base)
            equity += qty * mark
        return equity, unmarked

    def _mismatches(self, balances, positions, now: float) -> dict[str, dict[str, Decimal]]:
        by_asset: dict[str, list[str]] = {}
        for sym, r in self.risk.rules.items():
            by_asset.setdefault(r.base, []).append(sym)
        # Only a RECENT mid-submit intent explains a lag; a stuck one must not mask a mismatch.
        in_flight = {rec.intent.symbol for rec in self.store.intents_in_states(_IN_FLIGHT)
                     if now - rec.updated_at <= self.cfg.in_flight_grace_s}
        out = {}
        for asset, syms in sorted(by_asset.items()):
            if in_flight & set(syms):  # an order is mid-submit: the ledger may lag the exchange
                self._mismatch_streak[asset] = 0
                continue
            ledger = sum((self._qty(positions, s) for s in syms), _ZERO)
            exch = self._total(balances, asset)
            step = Decimal(1).scaleb(-max(self.risk.rules[s].amount_precision for s in syms))
            tol = max(step * self.cfg.mismatch_abs_units, ledger * self.cfg.mismatch_rel_pct / _HUNDRED)
            if abs(exch - ledger) > tol:
                self._mismatch_streak[asset] = self._mismatch_streak.get(asset, 0) + 1
            else:
                self._mismatch_streak[asset] = 0
            if self._mismatch_streak[asset] >= self.cfg.mismatch_confirm_ticks:
                out[asset] = {"exchange": exch, "ledger": ledger, "tolerance": tol}
        return out

    def _needs_review(self, what: str, detail: Any, now: float) -> None:
        payload = {"what": what, "detail": detail}
        self._audit("needs_review", payload, now)
        self._error("guardian.needs_review", f"operator review needed: {what}", now, payload)
        log.error("guardian: operator review needed: %s %s", what, detail)

    def _reconcile(self, now: float) -> None:
        try:
            self.oms.reconcile(now=now)
        except Exception as e:
            self._error("guardian.monitor", f"reconcile failed: {e!r}", now)

    @staticmethod
    def _total(balances, asset: str) -> Decimal:
        b = balances.get(asset)
        return _ZERO if b is None else b.free + b.locked

    @staticmethod
    def _qty(positions, sym: str) -> Decimal:
        p = positions.get(sym)
        return _ZERO if p is None else p.quantity

    def _audit(self, kind: str, payload: Any, now: float) -> None:
        self.store.append_audit_event(ACTOR, kind, payload, ts=now)

    def _error(self, source: str, message: str, now: float, payload: Any = None) -> None:
        try:
            self.store.append_error(source, message, payload, ts=now)
        except Exception as e:
            log.error("guardian error write failed: %r (%s)", e, message)

